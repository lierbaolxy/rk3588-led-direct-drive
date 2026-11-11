// SPDX-License-Identifier: GPL-2.0
#include <linux/atomic.h>
#include <linux/cpumask.h>
#include <linux/errno.h>
#include <linux/fs.h>
#include <linux/if_ether.h>
#include <linux/kthread.h>
#include <linux/ktime.h>
#include <linux/miscdevice.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/netdevice.h>
#include <linux/preempt.h>
#include <linux/sched.h>
#include <uapi/linux/sched/types.h>
#include <linux/skbuff.h>
#include <linux/slab.h>
#include <linux/uaccess.h>
#include <linux/wait.h>
#include <net/net_namespace.h>

#include "nova_kmod_uapi.h"

#define NOVA_TX_SLOTS 3
#define NOVA_TX_START_DELAY_NS 100000ULL

enum nova_frame_state {
	NOVA_FRAME_FREE = 0,
	NOVA_FRAME_BUILDING,
	NOVA_FRAME_READY,
	NOVA_FRAME_SENDING,
};

struct nova_frame {
	struct sk_buff **skbs;
	u32 count;
	u64 seq;
	enum nova_frame_state state;
};

struct nova_ctx {
	struct mutex lock;
	wait_queue_head_t wait;
	struct task_struct *thread;
	struct net_device *dev;
	struct nova_frame frames[NOVA_TX_SLOTS];
	struct nova_tx_packet *descs;
	struct nova_tx_config cfg;
	struct nova_tx_stats stats;
	u64 next_seq;
	bool configured;
	bool stopping;
};

static atomic_t nova_tx_open = ATOMIC_INIT(0);

static void nova_free_frame(struct nova_frame *frame)
{
	u32 i;

	if (!frame->skbs)
		return;
	for (i = 0; i < frame->count; i++) {
		if (frame->skbs[i]) {
			kfree_skb(frame->skbs[i]);
			frame->skbs[i] = NULL;
		}
	}
	frame->count = 0;
}

static void nova_free_context(struct nova_ctx *ctx)
{
	int i;

	for (i = 0; i < NOVA_TX_SLOTS; i++) {
		nova_free_frame(&ctx->frames[i]);
		kfree(ctx->frames[i].skbs);
	}
	if (ctx->dev)
		dev_put(ctx->dev);
	kfree(ctx->descs);
	kfree(ctx);
}

static bool nova_has_ready(struct nova_ctx *ctx)
{
	int i;
	bool ready = false;

	mutex_lock(&ctx->lock);
	for (i = 0; i < NOVA_TX_SLOTS; i++) {
		if (ctx->frames[i].state == NOVA_FRAME_READY) {
			ready = true;
			break;
		}
	}
	mutex_unlock(&ctx->lock);
	return ready;
}

static bool nova_has_free(struct nova_ctx *ctx)
{
	int i;
	bool free_slot = false;

	mutex_lock(&ctx->lock);
	for (i = 0; i < NOVA_TX_SLOTS; i++) {
		if (ctx->frames[i].state == NOVA_FRAME_FREE) {
			free_slot = true;
			break;
		}
	}
	mutex_unlock(&ctx->lock);
	return free_slot;
}

static struct nova_frame *nova_take_ready(struct nova_ctx *ctx)
{
	struct nova_frame *best = NULL;
	int i;

	mutex_lock(&ctx->lock);
	for (i = 0; i < NOVA_TX_SLOTS; i++) {
		struct nova_frame *frame = &ctx->frames[i];

		if (frame->state != NOVA_FRAME_READY)
			continue;
		if (!best || frame->seq < best->seq)
			best = frame;
	}
	if (best)
		best->state = NOVA_FRAME_SENDING;
	mutex_unlock(&ctx->lock);
	return best;
}

static void nova_wait_ns(struct nova_ctx *ctx, u64 target)
{
	while ((s64)(target - ktime_get_ns()) > 0) {
		if (READ_ONCE(ctx->stopping))
			return;
		cpu_relax();
	}
}

static void nova_record_late(struct nova_ctx *ctx, u64 late_ns)
{
	u64 us = late_ns / 1000ULL;
	u32 bin = us < 64 ? (u32)us : 64;

	ctx->stats.late_hist[bin]++;
	if (late_ns > ctx->stats.max_late_ns)
		ctx->stats.max_late_ns = late_ns;
}

static void nova_send_frame(struct nova_ctx *ctx, struct nova_frame *frame,
			    u64 frame_start)
{
	u32 i;

	for (i = 0; i < frame->count; i++) {
		struct sk_buff *skb;
		u64 target = frame_start + (u64)i * ctx->cfg.gap_ns;
		u64 now;
		int ret;

		if (READ_ONCE(ctx->stopping))
			break;
		nova_wait_ns(ctx, target);
		if (READ_ONCE(ctx->stopping))
			break;

		now = ktime_get_ns();
		nova_record_late(ctx, now > target ? now - target : 0);
		skb = frame->skbs[i];
		frame->skbs[i] = NULL;
		if (!skb)
			continue;

		if (ctx->cfg.flags & NOVA_TX_F_DIRECT)
			ret = dev_direct_xmit(skb, 0);
		else
			ret = dev_queue_xmit(skb);
		ctx->stats.packets++;
		if (ret != NETDEV_TX_OK)
			ctx->stats.errors++;
	}
}

static int nova_tx_thread(void *data)
{
	struct nova_ctx *ctx = data;
	struct sched_param param = { .sched_priority = 80 };
	u64 next_frame = 0;

	if (sched_setscheduler(current, SCHED_FIFO, &param))
		pr_warn("nova_tx: unable to set SCHED_FIFO\n");

	while (!kthread_should_stop() && !READ_ONCE(ctx->stopping)) {
		struct nova_frame *frame;
		u64 now;

		if (!nova_has_ready(ctx)) {
			next_frame = 0;
			wait_event_interruptible(ctx->wait,
				nova_has_ready(ctx) || READ_ONCE(ctx->stopping) ||
				kthread_should_stop());
			continue;
		}

		frame = nova_take_ready(ctx);
		if (!frame)
			continue;

		now = ktime_get_ns();
		if ((ctx->cfg.flags & NOVA_TX_F_PHASE_LOCK) &&
		    ctx->cfg.frame_period_ns && ctx->cfg.phase_epoch_ns) {
			/* 锚定到全局墙钟时隙，四路共用同一 epoch/period */
			u64 real_now = ktime_get_real_ns();
			u64 epoch = ctx->cfg.phase_epoch_ns;
			u64 period = ctx->cfg.frame_period_ns;
			u64 k, target_real;

			if (real_now >= epoch) {
				k = (real_now - epoch) / period + 1;
				target_real = epoch + k * period;
				next_frame = now + (target_real - real_now);
				ctx->stats.last_frame_real_ns = target_real;
			} else {
				next_frame = now + NOVA_TX_START_DELAY_NS;
			}
		} else if (!next_frame ||
		    (ctx->cfg.frame_period_ns &&
		     (s64)(now - next_frame) > ctx->cfg.frame_period_ns / 2)) {
			next_frame = now + NOVA_TX_START_DELAY_NS;
		}
		nova_wait_ns(ctx, next_frame);
		if ((ctx->cfg.flags & (NOVA_TX_F_DIRECT | NOVA_TX_F_NO_PREEMPT)) ==
		    (NOVA_TX_F_DIRECT | NOVA_TX_F_NO_PREEMPT))
			preempt_disable();
		nova_send_frame(ctx, frame, next_frame);
		if ((ctx->cfg.flags & (NOVA_TX_F_DIRECT | NOVA_TX_F_NO_PREEMPT)) ==
		    (NOVA_TX_F_DIRECT | NOVA_TX_F_NO_PREEMPT))
			preempt_enable();
		if (!READ_ONCE(ctx->stopping))
			ctx->stats.frames++;

		nova_free_frame(frame);
		mutex_lock(&ctx->lock);
		frame->state = NOVA_FRAME_FREE;
		if (ctx->stats.queue_depth)
			ctx->stats.queue_depth--;
		mutex_unlock(&ctx->lock);
		wake_up_interruptible(&ctx->wait);

		if (ctx->cfg.frame_period_ns)
			next_frame += ctx->cfg.frame_period_ns;
		else
			next_frame = 0;
	}
	return 0;
}

static int nova_tx_open_file(struct inode *inode, struct file *file)
{
	struct nova_ctx *ctx;

	if (atomic_cmpxchg(&nova_tx_open, 0, 1))
		return -EBUSY;
	ctx = kzalloc(sizeof(*ctx), GFP_KERNEL);
	if (!ctx) {
		atomic_set(&nova_tx_open, 0);
		return -ENOMEM;
	}
	mutex_init(&ctx->lock);
	init_waitqueue_head(&ctx->wait);
	file->private_data = ctx;
	return 0;
}

static long nova_tx_do_config(struct nova_ctx *ctx, unsigned long arg)
{
	struct nova_tx_config cfg;
	struct task_struct *thread;
	int i;
	int ret = 0;

	if (copy_from_user(&cfg, (void __user *)arg, sizeof(cfg)))
		return -EFAULT;
	cfg.ifname[NOVA_TX_IFNAMSIZ - 1] = '\0';
	if (!cfg.ifname[0] || !cfg.max_packets ||
	    cfg.max_packets > NOVA_TX_MAX_PACKETS ||
	    cfg.gap_ns > 1000000000U || cfg.frame_period_ns > 2000000000U ||
	    cfg.cpu < -1 || cfg.cpu >= nr_cpu_ids)
		return -EINVAL;
	if ((cfg.flags & NOVA_TX_F_PHASE_LOCK) &&
	    (!cfg.frame_period_ns || !cfg.phase_epoch_ns))
		return -EINVAL;
	if (cfg.cpu >= 0 && !cpu_online(cfg.cpu))
		return -ENODEV;

	mutex_lock(&ctx->lock);
	if (ctx->configured) {
		mutex_unlock(&ctx->lock);
		return -EBUSY;
	}
	ctx->configured = true;
	mutex_unlock(&ctx->lock);

	ctx->dev = dev_get_by_name(&init_net, cfg.ifname);
	if (!ctx->dev) {
		ret = -ENODEV;
		goto fail;
	}
	ctx->descs = kcalloc(cfg.max_packets, sizeof(*ctx->descs), GFP_KERNEL);
	if (!ctx->descs) {
		ret = -ENOMEM;
		goto fail;
	}
	for (i = 0; i < NOVA_TX_SLOTS; i++) {
		ctx->frames[i].skbs = kcalloc(cfg.max_packets,
					      sizeof(struct sk_buff *), GFP_KERNEL);
		if (!ctx->frames[i].skbs) {
			ret = -ENOMEM;
			goto fail;
		}
	}
	ctx->cfg = cfg;
	ctx->stopping = false;
	thread = kthread_create(nova_tx_thread, ctx, "nova_tx");
	if (IS_ERR(thread)) {
		ret = PTR_ERR(thread);
		goto fail;
	}
	ctx->thread = thread;
	if (cfg.cpu >= 0)
		kthread_bind(thread, cfg.cpu);
	wake_up_process(thread);
	return 0;

fail:
	for (i = 0; i < NOVA_TX_SLOTS; i++) {
		kfree(ctx->frames[i].skbs);
		ctx->frames[i].skbs = NULL;
	}
	if (ctx->dev) {
		dev_put(ctx->dev);
		ctx->dev = NULL;
	}
	kfree(ctx->descs);
	ctx->descs = NULL;
	mutex_lock(&ctx->lock);
	ctx->configured = false;
	mutex_unlock(&ctx->lock);
	return ret;
}

static long nova_tx_do_submit(struct nova_ctx *ctx, unsigned long arg)
{
	struct nova_tx_submit submit;
	struct nova_frame *frame = NULL;
	size_t desc_bytes;
	u32 i;
	int ret = 0;
	long wait_rc;

	if (copy_from_user(&submit, (void __user *)arg, sizeof(submit)))
		return -EFAULT;
	if (!ctx->configured || !submit.packets || !submit.count ||
	    submit.count > ctx->cfg.max_packets)
		return -EINVAL;

retry_free:
	mutex_lock(&ctx->lock);
	for (i = 0; i < NOVA_TX_SLOTS; i++) {
		if (ctx->frames[i].state == NOVA_FRAME_FREE) {
			frame = &ctx->frames[i];
			frame->state = NOVA_FRAME_BUILDING;
			break;
		}
	}
	mutex_unlock(&ctx->lock);
	if (!frame) {
		wait_rc = wait_event_interruptible(ctx->wait,
			nova_has_free(ctx) || READ_ONCE(ctx->stopping));
		if (READ_ONCE(ctx->stopping))
			return -ECANCELED;
		if (wait_rc < 0)
			return wait_rc;
		goto retry_free;
	}

	frame->count = 0;
	desc_bytes = submit.count * sizeof(*ctx->descs);
	if (copy_from_user(ctx->descs, (void __user *)(uintptr_t)submit.packets,
			   desc_bytes)) {
		ret = -EFAULT;
		goto fail;
	}

	for (i = 0; i < submit.count; i++) {
		struct nova_tx_packet *desc = &ctx->descs[i];
		struct sk_buff *skb;
		unsigned int len = desc->len;
		unsigned int headroom = LL_RESERVED_SPACE(ctx->dev) + 32;

		if (len < sizeof(struct ethhdr) || len > NOVA_TX_MAX_PACKET_LEN) {
			ret = -EINVAL;
			goto fail;
		}
		skb = alloc_skb(headroom + len, GFP_KERNEL);
		if (!skb) {
			ret = -ENOMEM;
			goto fail;
		}
		skb_reserve(skb, headroom);
		skb_put(skb, len);
		if (copy_from_user(skb->data,
				   (void __user *)(uintptr_t)desc->data, len)) {
			kfree_skb(skb);
			ret = -EFAULT;
			goto fail;
		}
		skb->dev = ctx->dev;
		skb->protocol = ((struct ethhdr *)skb->data)->h_proto;
		skb->pkt_type = PACKET_OUTGOING;
		skb->queue_mapping = 0;
		skb_reset_mac_header(skb);
		frame->skbs[i] = skb;
		frame->count = i + 1;
	}

	mutex_lock(&ctx->lock);
	frame->seq = ctx->next_seq++;
	frame->state = NOVA_FRAME_READY;
	ctx->stats.queue_depth++;
	mutex_unlock(&ctx->lock);
	wake_up_interruptible(&ctx->wait);
	return 0;

fail:
	nova_free_frame(frame);
	mutex_lock(&ctx->lock);
	frame->state = NOVA_FRAME_FREE;
	mutex_unlock(&ctx->lock);
	return ret;
}

static long nova_tx_ioctl(struct file *file, unsigned int cmd, unsigned long arg)
{
	struct nova_ctx *ctx = file->private_data;
	struct nova_tx_stats stats;

	switch (cmd) {
	case NOVA_TX_IOC_CONFIG:
		return nova_tx_do_config(ctx, arg);
	case NOVA_TX_IOC_SUBMIT:
		return nova_tx_do_submit(ctx, arg);
	case NOVA_TX_IOC_STATS:
		mutex_lock(&ctx->lock);
		memcpy(&stats, &ctx->stats, sizeof(stats));
		mutex_unlock(&ctx->lock);
		if (copy_to_user((void __user *)arg, &stats, sizeof(stats)))
			return -EFAULT;
		return 0;
	default:
		return -ENOTTY;
	}
}

static int nova_tx_release(struct inode *inode, struct file *file)
{
	struct nova_ctx *ctx = file->private_data;

	mutex_lock(&ctx->lock);
	ctx->stopping = true;
	mutex_unlock(&ctx->lock);
	wake_up_interruptible(&ctx->wait);
	if (ctx->thread)
		kthread_stop(ctx->thread);
	nova_free_context(ctx);
	atomic_set(&nova_tx_open, 0);
	return 0;
}

static const struct file_operations nova_tx_fops = {
	.owner = THIS_MODULE,
	.open = nova_tx_open_file,
	.unlocked_ioctl = nova_tx_ioctl,
	.release = nova_tx_release,
	.llseek = no_llseek,
};

static struct miscdevice nova_tx_miscdev = {
	.minor = MISC_DYNAMIC_MINOR,
	.name = NOVA_TX_DEVICE,
	.fops = &nova_tx_fops,
	.mode = 0600,
};

static int __init nova_tx_init(void)
{
	int ret = misc_register(&nova_tx_miscdev);

	pr_info("nova_tx: misc_register returned %d, minor=%d\n",
		ret, nova_tx_miscdev.minor);
	return ret;
}

static void __exit nova_tx_exit(void)
{
	misc_deregister(&nova_tx_miscdev);
	pr_info("nova_tx: unloaded\n");
}

module_init(nova_tx_init);
module_exit(nova_tx_exit);

MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("Nova LED kernel-paced raw Ethernet frame transmitter");
