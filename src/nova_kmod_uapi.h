#ifndef NOVA_KMOD_UAPI_H
#define NOVA_KMOD_UAPI_H

#include <linux/ioctl.h>
#include <linux/types.h>

#define NOVA_TX_DEVICE "nova_tx"
#define NOVA_TX_IFNAMSIZ 16
#define NOVA_TX_MAX_PACKETS 4096
#define NOVA_TX_MAX_PACKET_LEN 16384
#define NOVA_TX_HIST_BINS 65
#define NOVA_TX_F_DIRECT (1U << 0)
#define NOVA_TX_F_NO_PREEMPT (1U << 1)
#define NOVA_TX_F_PHASE_LOCK (1U << 2)

struct nova_tx_config {
	char ifname[NOVA_TX_IFNAMSIZ];
	__u32 max_packets;
	__u32 gap_ns;
	__u32 frame_period_ns;
	__s32 cpu;
	__u32 flags;
	__u64 phase_epoch_ns;
};

struct nova_tx_packet {
	__u64 data;
	__u32 len;
	__u32 reserved;
};

struct nova_tx_submit {
	__u64 packets;
	__u32 count;
	__u32 reserved;
};

struct nova_tx_stats {
	__u64 frames;
	__u64 packets;
	__u64 errors;
	__u64 dropped;
	__u64 queue_full;
	__u64 max_late_ns;
	__u64 last_frame_real_ns;
	__u32 queue_depth;
	__u32 reserved;
	__u64 late_hist[NOVA_TX_HIST_BINS];
	__u64 resends;
};

#define NOVA_TX_IOC_MAGIC 0xB7
#define NOVA_TX_IOC_CONFIG _IOW(NOVA_TX_IOC_MAGIC, 0, struct nova_tx_config)
#define NOVA_TX_IOC_SUBMIT _IOW(NOVA_TX_IOC_MAGIC, 1, struct nova_tx_submit)
#define NOVA_TX_IOC_STATS _IOR(NOVA_TX_IOC_MAGIC, 2, struct nova_tx_stats)

#endif
