// SPDX-License-Identifier: GPL-2.0
#include <linux/atomic.h>
#include <linux/cpumask.h>
#include <linux/errno.h>
#include <linux/fs.h>
#include <linux/if_ether.h>
#include <linux/jiffies.h>
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

/* 帧槽 5 个：预留更深的缓冲，供帧偶发抖动时用旧槽顶住，不触发补发 */
#define NOVA_TX_SLOTS 5
#define NOVA_TX_START_DELAY_NS 100000ULL
/* 帧间空窗上限：超过约 3ms 接收卡会丢锁闪黑，留 1ms 余量 */
#define NOVA_TX_MAX_GAP_NS 2000000ULL
/* 续发安全间隙：既能识别帧边界，又远小于丢锁阈值 */
#define NOVA_TX_RESUME_GAP_NS 1000000ULL
/* 相位锚定模式下补发决策点：时隙边界前预留时间复制备份帧。
 * 2026-11-05 由 8ms 收到 3ms：submit 复制 1080 个 skb 常超过 4ms，
 * 决策点太早会判定"没有新帧"，导致频繁补发（重复上一帧），
 * 多台补发时刻不一致时接缝处会明显错位 */
#define NOVA_TX_DECIDE_NS 3000000ULL
/* 帧尾簿记错过边界后的容忍窗口：边界刚过去 10ms 内仍立即发送，
 * 而不是空转一整个时隙（空转 = >30ms 断流 = 接收卡丢锁闪黑） */
#define NOVA_TX_LATE_USE_NS 10000000ULL
/* 等待空闲帧槽的上限：2026-11-05 凌晨三台设备因发送线程停摆，
 * submit 的 wait_event_interruptible 无超时，进程进入 D 状态杀不掉、
 * 模块也卸不掉，只能重启整机。加超时后最多等待这么久就返回 -EAGAIN，
 * 用户态按丢帧继续处理，服务仍可正常停止→rmmod→重新加载恢复 */
#define NOVA_TX_SLOT_WAIT_MS 12

enum nova_frame_state {
	NOVA_FRAME_FREE = 0,
	NOVA_FRAME_BUILDING,
	NOVA_FRAME_READY,
	NOVA_FRAME_SENDING,
};

struct nova_frame {
	struct sk_buff **skbs;
	struct sk_buff **skbs_dup;	/* 备份套：供断流时补发上一帧 */
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
	struct sk_buff **resend;	/* 最近发送帧的备份套 */
	struct sk_buff **copy_skbs;	/* 补发时的复制暂存 */
	u32 resend_count;
	struct sk_buff **trash;		/* 延迟释放队列：submit 时腾空 */
	u32 trash_count;
	u32 trash_cap;
	u64 next_seq;
	bool configured;
	bool stopping;
};

static atomic_t nova_tx_open = ATOMIC_INIT(0);

static void nova_trash_push(struct nova_ctx *ctx, struct sk_buff **skbs,
			    u32 count);
static void nova_empty_trash(struct nova_ctx *ctx);

static void nova_free_skb_set(struct sk_buff **skbs, u32 count)
{
	u32 i;

	if (!skbs)
		return;
	for (i = 0; i < count; i++) {
		if (skbs[i]) {
			kfree_skb(skbs[i]);
			skbs[i] = NULL;
		}
	}
}

static void nova_free_frame(struct nova_frame *frame)
{
	nova_free_skb_set(frame->skbs, frame->count);
	nova_free_skb_set(frame->skbs_dup, frame->count);
	frame->count = 0;
}

static void nova_free_context(struct nova_ctx *ctx)
{
	int i;

	for (i = 0; i < NOVA_TX_SLOTS; i++) {
		nova_free_frame(&ctx->frames[i]);
		kfree(ctx->frames[i].skbs);
		kfree(ctx->frames[i].skbs_dup);
	}
	nova_free_skb_set(ctx->resend, ctx->resend_count);
	kfree(ctx->resend);
	kfree(ctx->copy_skbs);
	nova_empty_trash(ctx);
	kfree(ctx->trash);
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

/* 旧备份套挂入延迟释放队列，避免在帧尾关键路径上成批 kfree_skb。
 * trash 由 submit（进程上下文）腾空；溢出时退化为立即释放 */
static void nova_trash_push(struct nova_ctx *ctx, struct sk_buff **skbs, u32 count)
{
	if (!skbs || !count)
		return;
	if (!ctx->trash || ctx->trash_count + count > ctx->trash_cap) {
		nova_free_skb_set(skbs, count);
		return;
	}
	memcpy(ctx->trash + ctx->trash_count, skbs,
	       count * sizeof(struct sk_buff *));
	ctx->trash_count += count;
	memset(skbs, 0, count * sizeof(struct sk_buff *));
}

static void nova_empty_trash(struct nova_ctx *ctx)
{
	if (!ctx->trash || !ctx->trash_count)
		return;
	nova_free_skb_set(ctx->trash, ctx->trash_count);
	ctx->trash_count = 0;
}

/* 新帧发送后，把它的备份套轮换为补发用副本。
 * 注意：只能搬运 skb 指针，槽位的 skbs_dup 数组本身必须保留，
 * 否则该槽位下一次 submit 会空指针解引用 */
static void nova_adopt_resend(struct nova_ctx *ctx, struct nova_frame *frame)
{
	nova_trash_push(ctx, ctx->resend, ctx->resend_count);
	ctx->resend_count = 0;
	if (!frame->skbs_dup)
		return;
	memcpy(ctx->resend, frame->skbs_dup,
	       frame->count * sizeof(struct sk_buff *));
	memset(frame->skbs_dup, 0, frame->count * sizeof(struct sk_buff *));
	ctx->resend_count = frame->count;
}

/* 断流补发：从备份套复制出一套 skb，仍然锚定在同一全局时隙边界发送，
 * 保证相位恒定、帧间空窗恒定，接收卡不会丢锁闪黑 */
static u32 nova_build_resend(struct nova_ctx *ctx)
{
	u32 i;

	for (i = 0; i < ctx->resend_count; i++) {
		struct sk_buff *skb;

		if (!ctx->resend[i])
			break;
		skb = skb_copy(ctx->resend[i], GFP_ATOMIC);
		if (!skb)
			break;
		skb->dev = ctx->dev;
		skb->protocol = ((struct ethhdr *)skb->data)->h_proto;
		skb->pkt_type = PACKET_OUTGOING;
		skb->queue_mapping = 0;
		skb_reset_mac_header(skb);
		ctx->copy_skbs[i] = skb;
	}
	return i;
}

static void nova_send_frame(struct nova_ctx *ctx, struct nova_frame *frame,
			    u64 frame_start)
{
	u32 i;
	u64 target = frame_start;

	for (i = 0; i < frame->count; i++) {
		struct sk_buff *skb;
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

		target += ctx->cfg.gap_ns;
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
		struct nova_frame resend_view;
		bool is_resend = false;
		u64 now;

		if ((ctx->cfg.flags & NOVA_TX_F_PHASE_LOCK) &&
		    ctx->cfg.frame_period_ns && ctx->cfg.phase_epoch_ns) {
			/* 严格时隙网格：每个全局时隙边界必发一帧。
			 * 有新帧发新帧；供帧断流时补发上一帧备份，
			 * 相位与帧间空窗恒定，接收卡不丢锁、四路不错位 */
			u64 real_now = ktime_get_real_ns();
			u64 epoch = ctx->cfg.phase_epoch_ns;
			u64 period = ctx->cfg.frame_period_ns;
			u64 k, target_real, target_mono, decide;

			now = ktime_get_ns();
			if (real_now < epoch) {
				/* 墙钟尚未到 epoch：等第一帧，走普通立即发送 */
				if (!nova_has_ready(ctx)) {
					wait_event_interruptible(ctx->wait,
						nova_has_ready(ctx) ||
						READ_ONCE(ctx->stopping) ||
						kthread_should_stop());
					continue;
				}
				frame = nova_take_ready(ctx);
				if (!frame)
					continue;
				next_frame = now + NOVA_TX_START_DELAY_NS;
				goto send_new;
			}

			k = (real_now - epoch) / period + 1;
			target_real = epoch + k * period;
			/* 边界刚过去不久：帧尾簿记耽误了起点，立即补发本时隙，
			 * 绝不能空转一整个时隙（空转 = >30ms 断流 = 闪黑） */
			if ((s64)(real_now - (target_real - period)) >= 0 &&
			    (s64)(real_now - (target_real - period)) <
			    NOVA_TX_LATE_USE_NS)
				target_real -= period;
			target_mono = now + (target_real - real_now);

			frame = nova_take_ready(ctx);
			if (!frame) {
				/* 给供帧端留到决策点：边界前 8ms 内有新帧仍用新帧 */
				s64 wait_ns;

				decide = target_mono - NOVA_TX_DECIDE_NS;
				wait_ns = (s64)(decide - ktime_get_ns());
				if (wait_ns > 0)
					wait_event_interruptible_hrtimeout(ctx->wait,
						nova_has_ready(ctx) ||
						READ_ONCE(ctx->stopping) ||
						kthread_should_stop(),
						ns_to_ktime(wait_ns));
				frame = nova_take_ready(ctx);
			}

			if (frame) {
				next_frame = target_mono;
				goto send_new;
			}

			if (!ctx->resend || !ctx->resend_count) {
				/* 启动初期还没有任何帧可补发，等第一帧。
				 * resend 数组在 config 时已分配，必须看 resend_count，
				 * 否则会发出 count=0 的空帧并紧循环空转 */
				next_frame = 0;
				wait_event_interruptible(ctx->wait,
					nova_has_ready(ctx) ||
					READ_ONCE(ctx->stopping) ||
					kthread_should_stop());
				continue;
			}

			if (nova_build_resend(ctx) != ctx->resend_count) {
				nova_free_skb_set(ctx->copy_skbs, ctx->resend_count);
				ctx->stats.errors++;
				continue;
			}
			memset(&resend_view, 0, sizeof(resend_view));
			resend_view.skbs = ctx->copy_skbs;
			resend_view.count = ctx->resend_count;
			frame = &resend_view;
			is_resend = true;
			next_frame = target_mono;
			ctx->stats.last_frame_real_ns = target_real;
		} else {
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
			if (!next_frame ||
			    (ctx->cfg.frame_period_ns &&
			     (s64)(now - next_frame) > ctx->cfg.frame_period_ns / 2))
				next_frame = now + NOVA_TX_START_DELAY_NS;
		}

send_new:
		if ((ctx->cfg.flags & NOVA_TX_F_PHASE_LOCK) &&
		    ctx->cfg.frame_period_ns && ctx->cfg.phase_epoch_ns) {
			/* 先睡到目标前 500µs 再忙等收尾：避免发送线程
			 * 长时间 100% 占满 CPU6 饿死看门狗线程 */
			while (!READ_ONCE(ctx->stopping) && !kthread_should_stop()) {
				s64 left = (s64)(next_frame - ktime_get_ns());
				ktime_t wake;

				if (left <= 500000)
					break;
				wake = ns_to_ktime(next_frame - 500000);
				set_current_state(TASK_INTERRUPTIBLE);
				schedule_hrtimeout(&wake, HRTIMER_MODE_ABS);
			}
			__set_current_state(TASK_RUNNING);
		}
		nova_wait_ns(ctx, next_frame);
		if ((ctx->cfg.flags & (NOVA_TX_F_DIRECT | NOVA_TX_F_NO_PREEMPT)) ==
		    (NOVA_TX_F_DIRECT | NOVA_TX_F_NO_PREEMPT))
			preempt_disable();
		nova_send_frame(ctx, frame, next_frame);
		if ((ctx->cfg.flags & (NOVA_TX_F_DIRECT | NOVA_TX_F_NO_PREEMPT)) ==
		    (NOVA_TX_F_DIRECT | NOVA_TX_F_NO_PREEMPT))
			preempt_enable();
		if (!READ_ONCE(ctx->stopping)) {
			ctx->stats.frames++;
			if (is_resend)
				ctx->stats.resends++;
		}

		if (is_resend) {
			/* 备份套仍保留在 ctx->resend，复制套已被发送消费 */
			nova_free_skb_set(ctx->copy_skbs, ctx->resend_count);
			continue;
		}

		if ((ctx->cfg.flags & NOVA_TX_F_PHASE_LOCK) &&
		    ctx->cfg.frame_period_ns && ctx->cfg.phase_epoch_ns) {
			nova_adopt_resend(ctx, frame);
			ctx->stats.last_frame_real_ns =
				ktime_get_real_ns() - (ktime_get_ns() - next_frame);
		}

		nova_free_frame(frame);
		mutex_lock(&ctx->lock);
		frame->state = NOVA_FRAME_FREE;
		if (ctx->stats.queue_depth)
			ctx->stats.queue_depth--;
		mutex_unlock(&ctx->lock);
		wake_up_interruptible(&ctx->wait);

		if (!(ctx->cfg.flags & NOVA_TX_F_PHASE_LOCK) &&
		    ctx->cfg.frame_period_ns)
			next_frame += ctx->cfg.frame_period_ns;
		else if (!ctx->cfg.frame_period_ns)
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
	ctx->copy_skbs = kcalloc(cfg.max_packets, sizeof(struct sk_buff *),
				 GFP_KERNEL);
	if (!ctx->copy_skbs) {
		ret = -ENOMEM;
		goto fail;
	}
	ctx->resend = kcalloc(cfg.max_packets, sizeof(struct sk_buff *),
			      GFP_KERNEL);
	if (!ctx->resend) {
		ret = -ENOMEM;
		goto fail;
	}
	ctx->trash_cap = cfg.max_packets * 6;
	ctx->trash = kcalloc(ctx->trash_cap, sizeof(struct sk_buff *),
			     GFP_KERNEL);
	if (!ctx->trash) {
		ret = -ENOMEM;
		goto fail;
	}
	for (i = 0; i < NOVA_TX_SLOTS; i++) {
		ctx->frames[i].skbs = kcalloc(cfg.max_packets,
					      sizeof(struct sk_buff *), GFP_KERNEL);
		ctx->frames[i].skbs_dup = kcalloc(cfg.max_packets,
						  sizeof(struct sk_buff *), GFP_KERNEL);
		if (!ctx->frames[i].skbs || !ctx->frames[i].skbs_dup) {
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
		kfree(ctx->frames[i].skbs_dup);
		ctx->frames[i].skbs_dup = NULL;
	}
	kfree(ctx->copy_skbs);
	kfree(ctx->resend);
	kfree(ctx->trash);
	ctx->copy_skbs = NULL;
	ctx->resend = NULL;
	ctx->trash = NULL;
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

	/* 进程上下文腾空延迟释放队列，卸掉发送线程帧尾的释放负担 */
	nova_empty_trash(ctx);

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
		wait_rc = wait_event_interruptible_timeout(ctx->wait,
			nova_has_free(ctx) || READ_ONCE(ctx->stopping),
			msecs_to_jiffies(NOVA_TX_SLOT_WAIT_MS));
		if (READ_ONCE(ctx->stopping))
			return -ECANCELED;
		if (wait_rc < 0)
			return wait_rc;
		if (wait_rc == 0) {
			ctx->stats.queue_full++;
			return -EAGAIN;
		}
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
		struct sk_buff *dup;
		unsigned int len = desc->len;
		unsigned int headroom = LL_RESERVED_SPACE(ctx->dev) + 32;

		if (len < sizeof(struct ethhdr) || len > NOVA_TX_MAX_PACKET_LEN) {
			ret = -EINVAL;
			goto fail;
		}
		/* 先用 GFP_ATOMIC：submit 在进程上下文但 1081 次分配若落入
		 * 直接内存回收，单帧提交会拖到 5ms 以上，实际发送流跟不上
		 * 时隙边界就触发补发。小对象原子分配命中率高，失败再回退 */
		skb = alloc_skb(headroom + len, GFP_ATOMIC);
		if (!skb)
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
		/* 备份套：供断流时补发上一帧，保持相位与空窗恒定 */
		dup = skb_copy(skb, GFP_KERNEL);
		if (!dup) {
			kfree_skb(skb);
			ret = -ENOMEM;
			goto fail;
		}
		frame->skbs[i] = skb;
		frame->skbs_dup[i] = dup;
		frame->count = i + 1;
	}

	mutex_lock(&ctx->lock);
	frame->seq = ctx->next_seq++;
	frame->state = NOVA_FRAME_READY;
	/* 只保留最新帧：丢弃尚未发送的旧帧，避免四路各自停在不同的旧画面上 */
	for (i = 0; i < NOVA_TX_SLOTS; i++) {
		struct nova_frame *old = &ctx->frames[i];

		if (old != frame && old->state == NOVA_FRAME_READY) {
			nova_free_frame(old);
			old->state = NOVA_FRAME_FREE;
			ctx->stats.dropped++;
		}
	}
	ctx->stats.queue_depth = 1;
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
