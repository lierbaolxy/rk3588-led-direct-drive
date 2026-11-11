#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <linux/if_ether.h>
#include <linux/if_packet.h>
#include <net/if.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#include "nova_kmod_uapi.h"

#define BATCH 64
#define HEADER_LEN 43
#define LINE_TYPE 0x1E
#define FIELD_TYPE 0x2D

static const uint8_t DST_MAC[6] = {0x12, 0x34, 0x56, 0x78, 0x9A, 0xBC};
static const uint8_t PROTO_HEADER[HEADER_LEN] = {
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0x55, 0x55, 0x55, 0x55, 0x55, 0x55, 0x55, 0xD5, 0x09
};

static long long ts_diff_ns(const struct timespec *a, const struct timespec *b) {
    return (long long)(a->tv_sec - b->tv_sec) * 1000000000LL
         + (a->tv_nsec - b->tv_nsec);
}

static struct timespec ts_add_ns(struct timespec t, long long ns) {
    t.tv_sec += ns / 1000000000LL;
    t.tv_nsec += ns % 1000000000LL;
    if (t.tv_nsec >= 1000000000L) {
        t.tv_sec++;
        t.tv_nsec -= 1000000000L;
    }
    return t;
}

static int read_full(int fd, uint8_t *buf, size_t len) {
    size_t done = 0;
    while (done < len) {
        ssize_t n = read(fd, buf + done, len - done);
        if (n > 0) {
            done += (size_t)n;
        } else if (n == 0) {
            return 0;
        } else if (errno != EINTR) {
            perror("读取桌面帧失败");
            return -1;
        }
    }
    return 1;
}

static size_t make_packet(uint8_t *out, const uint8_t src[6], uint8_t type,
                          const uint8_t *data, size_t data_len) {
    memcpy(out, DST_MAC, 6);
    memcpy(out + 6, src, 6);
    out[12] = 0xFF;
    out[13] = 0x00;
    memcpy(out + 14, PROTO_HEADER, HEADER_LEN);
    out[14 + HEADER_LEN] = type;
    if (data_len > 0) memcpy(out + 15 + HEADER_LEN, data, data_len);
    return 15 + HEADER_LEN + data_len;
}

static void make_field_data(unsigned n, uint8_t *data, size_t *len) {
    if (n == 1) {
        memset(data, 0, 538);
        data[0] = 0x01;
        data[1] = 0xFE;
        data[2] = 0x00;
        data[3] = 0xFF;
        *len = 538;
        return;
    }

    memset(data, 0, 1028);
    *len = 1028;
    if (n == 0) {
        data[0] = 0x00;
        data[1] = 0xFF;
        data[2] = 0x00;
        data[3] = 0xFF;
        for (int i = 5; i < 67; i += 2) {
            data[i] = 0x00;
            data[i + 1] = 0xFF;
        }
    } else if (n < 8) {
        data[0] = n & 0xFF;
        data[1] = (n >> 8) & 0xFF;
        data[2] = 0x00;
        data[3] = 0xFF;
    }
}

static int open_sender(const char *iface, uint8_t src[6], int *ifindex) {
    int fd = socket(AF_PACKET, SOCK_RAW, htons(ETH_P_ALL));
    if (fd < 0) {
        perror("创建原始网口失败（需要 sudo/root）");
        return -1;
    }

    struct ifreq req;
    memset(&req, 0, sizeof(req));
    snprintf(req.ifr_name, IFNAMSIZ, "%s", iface);
    if (ioctl(fd, SIOCGIFHWADDR, &req) < 0) {
        perror("读取网口 MAC 失败");
        close(fd);
        return -1;
    }
    memcpy(src, req.ifr_hwaddr.sa_data, 6);
    *ifindex = if_nametoindex(iface);
    if (!*ifindex) {
        perror("查找网口失败");
        close(fd);
        return -1;
    }

    struct sockaddr_ll local;
    memset(&local, 0, sizeof(local));
    local.sll_family = AF_PACKET;
    local.sll_protocol = htons(ETH_P_ALL);
    local.sll_ifindex = *ifindex;
    if (bind(fd, (struct sockaddr *)&local, sizeof(local)) < 0) {
        perror("绑定网口失败");
        close(fd);
        return -1;
    }

    int sndbuf = 32 * 1024 * 1024;
    setsockopt(fd, SOL_SOCKET, SO_SNDBUF, &sndbuf, sizeof(sndbuf));

    /* NOVA_QDISC_BYPASS=1：绕过 qdisc 直接交给驱动 ring。
     * 逐包 sendto 走 qdisc 时每次约 20-50µs，追不上 17-30µs 的包间隔；
     * 绕过 qdisc 可显著降低单包开销。 */
    if (getenv("NOVA_QDISC_BYPASS")) {
        int one = 1;
        if (setsockopt(fd, SOL_PACKET, PACKET_QDISC_BYPASS, &one, sizeof(one)) == 0) {
            fprintf(stderr, "已开启 PACKET_QDISC_BYPASS\n");
        } else {
            perror("设置 PACKET_QDISC_BYPASS 失败");
        }
    }
    return fd;
}

static int read_src_mac(const char *iface, uint8_t src[6]) {
    int fd = socket(AF_INET, SOCK_DGRAM, 0);
    struct ifreq req;

    if (fd < 0) return -1;
    memset(&req, 0, sizeof(req));
    snprintf(req.ifr_name, IFNAMSIZ, "%s", iface);
    if (ioctl(fd, SIOCGIFHWADDR, &req) < 0) {
        perror("读取网口 MAC 失败");
        close(fd);
        return -1;
    }
    memcpy(src, req.ifr_hwaddr.sa_data, 6);
    close(fd);
    return 0;
}

static int open_kmod_sender(const char *iface, int max_packets,
                            long long period_ns, long long gap_ns) {
    const char *path = getenv("NOVA_KMOD_DEV");
    struct nova_tx_config cfg;
    int fd;

    if (period_ns < 0 || period_ns > 2000000000LL ||
        gap_ns < 0 || gap_ns > 1000000000LL) {
        errno = EINVAL;
        perror("内核发送时间参数无效");
        return -1;
    }
    if (!path) path = "/dev/nova_tx";
    fd = open(path, O_RDWR);
    if (fd < 0) {
        perror("打开内核发送设备失败（需先加载 nova_kmod.ko）");
        return -1;
    }
    memset(&cfg, 0, sizeof(cfg));
    snprintf(cfg.ifname, sizeof(cfg.ifname), "%s", iface);
    cfg.max_packets = (uint32_t)max_packets;
    cfg.gap_ns = (uint32_t)gap_ns;
    cfg.frame_period_ns = (uint32_t)period_ns;
    cfg.cpu = getenv("NOVA_KMOD_CPU") ? atoi(getenv("NOVA_KMOD_CPU")) : 6;
    cfg.flags = getenv("NOVA_KMOD_QDISC") ? 0 : NOVA_TX_F_DIRECT;
    if (getenv("NOVA_KMOD_NO_PREEMPT")) cfg.flags |= NOVA_TX_F_NO_PREEMPT;
    if (getenv("NOVA_PHASE_LOCK") || getenv("NOVA_PHASE_EPOCH")) {
        const char *epoch = getenv("NOVA_PHASE_EPOCH");
        double epoch_s = epoch ? atof(epoch) : 0.0;
        if (epoch_s > 0.0) {
            cfg.flags |= NOVA_TX_F_PHASE_LOCK;
            cfg.phase_epoch_ns = (uint64_t)(epoch_s * 1e9);
        }
    }
    if (ioctl(fd, NOVA_TX_IOC_CONFIG, &cfg) < 0) {
        perror("配置内核发送设备失败");
        close(fd);
        return -1;
    }
    fprintf(stderr, "内核发送: %s gap=%lluus period=%lluns cpu=%d %s%s%s\n",
            iface, (unsigned long long)(gap_ns / 1000), period_ns, cfg.cpu,
            cfg.flags & NOVA_TX_F_DIRECT ? "direct-xmit" : "qdisc",
            cfg.flags & NOVA_TX_F_NO_PREEMPT ? " no-preempt" : "",
            cfg.flags & NOVA_TX_F_PHASE_LOCK ? " phase-lock" : "");
    return fd;
}

static int submit_kmod_frame(int fd, struct nova_tx_packet *descs,
                             uint8_t **packets, size_t *lengths, int count) {
    struct nova_tx_submit submit;

    for (int i = 0; i < count; i++) {
        descs[i].data = (uintptr_t)packets[i];
        descs[i].len = (uint32_t)lengths[i];
        descs[i].reserved = 0;
    }
    memset(&submit, 0, sizeof(submit));
    submit.packets = (uintptr_t)descs;
    submit.count = (uint32_t)count;
    if (ioctl(fd, NOVA_TX_IOC_SUBMIT, &submit) < 0) return -errno;
    return 0;
}

static double kmod_late_p999(const struct nova_tx_stats *stats) {
    uint64_t rank = stats->packets * 999 / 1000;
    uint64_t cumulative = 0;

    if (!rank) return 0.0;
    for (int i = 0; i < NOVA_TX_HIST_BINS; i++) {
        cumulative += stats->late_hist[i];
        if (cumulative >= rank) return i == 64 ? 64.0 : (double)i;
    }
    return 0.0;
}

/* 批量提交整帧：sendmmsg 把 1081 个包一次性交给内核，
 * 由网卡按线速（约 12µs/包）均匀发出，形成 13ms 的连续突发。
 * 逐包 sendto 每次系统调用约 20-50µs，远超 17µs 的包间隔，
 * 会导致包在驱动队列积压后突发弹出（实测间隔 4-52µs 乱跳），
 * 接收卡因此判定数据流异常而拒收。 */
static int send_packets_batch(int fd, int ifindex, uint8_t **packets,
                              size_t *lengths, int count, int gap_us) {
    struct sockaddr_ll addr;
    memset(&addr, 0, sizeof(addr));
    addr.sll_family = AF_PACKET;
    addr.sll_ifindex = ifindex;
    addr.sll_halen = ETH_ALEN;
    memcpy(addr.sll_addr, DST_MAC, ETH_ALEN);

    struct mmsghdr *msgs = calloc((size_t)count, sizeof(struct mmsghdr));
    struct iovec *iov = calloc((size_t)count, sizeof(struct iovec));
    if (!msgs || !iov) {
        free(msgs);
        free(iov);
        fprintf(stderr, "sendmmsg 结构分配失败\n");
        return -1;
    }
    for (int i = 0; i < count; i++) {
        iov[i].iov_base = packets[i];
        iov[i].iov_len = lengths[i];
        msgs[i].msg_hdr.msg_name = &addr;
        msgs[i].msg_hdr.msg_namelen = sizeof(addr);
        msgs[i].msg_hdr.msg_iov = &iov[i];
        msgs[i].msg_hdr.msg_iovlen = 1;
    }

    int sent = 0;
    struct timespec t0, now, target;
    clock_gettime(CLOCK_MONOTONIC, &t0);
    while (sent < count) {
        int batch = (count - sent > 8) ? 8 : (count - sent);
        int n = sendmmsg(fd, msgs + sent, (unsigned)batch, 0);
        if (n <= 0) {
            perror("sendmmsg 发送失败");
            free(msgs);
            free(iov);
            return -1;
        }
        sent += n;
        /* 按 gap_us 的节奏提交下一批：让网卡 FIFO 始终有积压，
         * 输出保持均匀（整帧一次性提交会让网卡发完后长时间空档）。 */
        if (gap_us > 0) {
            target = ts_add_ns(t0, (long long)sent * gap_us * 1000LL);
            do {
                clock_gettime(CLOCK_MONOTONIC, &now);
            } while (ts_diff_ns(&target, &now) > 0);
        }
    }
    free(msgs);
    free(iov);
    return 0;
}

static int send_packets(int fd, int ifindex, uint8_t **packets,
                        size_t *lengths, int count, int gap_us, int blank_us,
                        long long slot_ns) {
    static struct sockaddr_ll addr;
    memset(&addr, 0, sizeof(addr));
    addr.sll_family = AF_PACKET;
    addr.sll_ifindex = ifindex;
    addr.sll_halen = ETH_ALEN;
    memcpy(addr.sll_addr, DST_MAC, ETH_ALEN);

    /* slot_ns>0：把整帧所有包均匀铺满一个帧周期，与接收卡扫描节拍对齐 */
    struct timespec base;
    clock_gettime(CLOCK_MONOTONIC, &base);

    /* gap_us>0：逐包精确间隔。nanosleep 在 Linux 上最小粒度约 50µs，
     * 会把逐包节奏变成微突发，导致接收卡丢行，所以这里用忙等对齐绝对时刻。 */
    const int group = 1;
    struct timespec pkt_next;
    clock_gettime(CLOCK_MONOTONIC, &pkt_next);

    /* 实测抖动：相邻两包 sendto 完成时刻的间隔，以及相对计划时刻的滞后量 */
    static long long st_n, st_sum, st_min, st_max, st_lmin, st_lmax, st_lsum, st_last;
    static long long st_prev = -1;
    static unsigned long bk[8];  /* <5,5-10,10-15,15-19,19-23,23-30,30-100,>100 µs */
    long long sched_ns = (long long)pkt_next.tv_sec * 1000000000LL + pkt_next.tv_nsec;
    st_prev = -1;  /* 每帧第一包不计间隔，避免把帧间空档算进来 */

    for (int i = 0; i < count; i++) {
        ssize_t sent = sendto(fd, packets[i], lengths[i], 0,
                              (const struct sockaddr *)&addr, sizeof(addr));
        if (sent < 0) {
            if (errno == EINTR) {
                i--;
                continue;
            }
            perror("发送数据包失败");
            return -1;
        }
        if (gap_us > 0 && getenv("NOVA_JITTER")) {
            struct timespec td;
            clock_gettime(CLOCK_MONOTONIC, &td);
            long long done = (long long)td.tv_sec * 1000000000LL + td.tv_nsec;
            if (st_prev >= 0) {
                long long d = done - st_prev;
                st_n++;
                st_sum += d;
                if (st_min == 0 || d < st_min) st_min = d;
                if (d > st_max) st_max = d;
                long long us = d / 1000;
                int b = us < 5 ? 0 : us < 10 ? 1 : us < 15 ? 2 : us < 19 ? 3
                        : us < 23 ? 4 : us < 30 ? 5 : us < 100 ? 6 : 7;
                bk[b]++;
            }
            long long late = done - sched_ns;
            st_lsum += late;
            if (st_lmin == 0 || late < st_lmin) st_lmin = late;
            if (late > st_lmax) st_lmax = late;
            st_prev = done;
            if (st_last == 0) st_last = done;
            if (done - st_last >= 1000000000LL) {
                fprintf(stderr,
                        "JIT 间隔 min/avg/max = %.1f/%.1f/%.1f us | 落后 %.1f/%.1f/%.1f us | 分布 "
                        "<5:%lu 5-10:%lu 10-15:%lu 15-19:%lu 19-23:%lu 23-30:%lu 30-100:%lu >100:%lu\n",
                        st_min / 1000.0, (double)st_sum / st_n / 1000.0, st_max / 1000.0,
                        st_lmin / 1000.0, (double)st_lsum / st_n / 1000.0, st_lmax / 1000.0,
                        bk[0], bk[1], bk[2], bk[3], bk[4], bk[5], bk[6], bk[7]);
                st_n = st_sum = st_min = st_max = st_lsum = st_lmin = st_lmax = 0;
                memset(bk, 0, sizeof(bk));
                st_last = done;
            }
        }
        if (slot_ns > 0) {
            struct timespec target = ts_add_ns(base, (long long)(i + 1) * slot_ns);
            clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &target, NULL);
        } else if (gap_us > 0) {
            sched_ns += (long long)group * gap_us * 1000LL;
            /* 卡顿超过 25µs 就重新对齐，不做“连发一串追回来”，避免微突发打断接收卡 */
            if (getenv("NOVA_NOCATCH")) {
                struct timespec tn;
                clock_gettime(CLOCK_MONOTONIC, &tn);
                long long now_ns = (long long)tn.tv_sec * 1000000000LL + tn.tv_nsec;
                if (now_ns - sched_ns > 25000LL) sched_ns = now_ns;
            }
            struct timespec target;
            target.tv_sec = sched_ns / 1000000000LL;
            target.tv_nsec = sched_ns % 1000000000LL;
            struct timespec now;
            do {
                clock_gettime(CLOCK_MONOTONIC, &now);
            } while (ts_diff_ns(&target, &now) > 0);
        }
    }

    /* 场包之后留一段垂直消隐，给接收卡复位行计数，否则每帧顶部会丢失若干行 */
    if (blank_us > 0) {
        struct timespec t;
        clock_gettime(CLOCK_MONOTONIC, &t);
        t.tv_nsec += (long)blank_us * 1000L;
        while (t.tv_nsec >= 1000000000L) {
            t.tv_nsec -= 1000000000L;
            t.tv_sec++;
        }
        clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &t, NULL);
    }
    return 0;
}

int main(int argc, char **argv) {
    if (argc < 2 || argc > 7) {
        fprintf(stderr, "用法: %s <网口> [宽=480] [高=1080] [fps=60，0=外部节奏] [包间隔us=0] [场消隐us=0]\n", argv[0]);
        fprintf(stderr, "从标准输入读取 RGB24 桌面帧，例如 ffmpeg ... -pix_fmt rgb24 -f rawvideo - | %s eth0\n", argv[0]);
        return 2;
    }

    const char *iface = argv[1];
    int width = argc > 2 ? atoi(argv[2]) : 480;
    int height = argc > 3 ? atoi(argv[3]) : 1080;
    double fps = argc > 4 ? atof(argv[4]) : 60.0;
    int gap_us = argc > 5 ? atoi(argv[5]) : 0;
    int blank_us = argc > 6 ? atoi(argv[6]) : 0;
    if (width <= 0 || height <= 0 || fps < 0 || width > 4096 || height > 4096) {
        fprintf(stderr, "分辨率或帧率参数无效\n");
        return 2;
    }

    const int use_kmod = getenv("NOVA_KMOD") != NULL;
    uint8_t src[6];
    int ifindex = 0;
    int fd = -1;
    int kfd = -1;
    if (use_kmod) {
        if (read_src_mac(iface, src) < 0) return 1;
    } else {
        fd = open_sender(iface, src, &ifindex);
        if (fd < 0) return 1;
    }

    /* 行号注入实验参数（默认关闭） */
    int row_off = getenv("NOVA_ROW_OFF") ? atoi(getenv("NOVA_ROW_OFF")) : -1;
    int row_bytes = getenv("NOVA_ROW_BYTES") ? atoi(getenv("NOVA_ROW_BYTES")) : 2;
    int row_le = getenv("NOVA_ROW_LE") ? atoi(getenv("NOVA_ROW_LE")) : 0;
    int row_base = getenv("NOVA_ROW_BASE") ? atoi(getenv("NOVA_ROW_BASE")) : 0;
    if (row_off >= 0)
        fprintf(stderr, "行号注入: 包头偏移 %d, %d 字节, %s, 基准 %d\n",
                row_off, row_bytes, row_le ? "小端" : "大端", row_base);

    const size_t stride = (size_t)width * 3;
    const size_t frame_bytes = stride * height;
    const size_t line_len = 15 + HEADER_LEN + stride;
    uint8_t *frame = malloc(frame_bytes);
    uint8_t **lines = calloc(height, sizeof(*lines));
    uint8_t **packets = calloc(height + 1, sizeof(*packets));
    size_t *lengths = calloc(height + 1, sizeof(*lengths));
    uint8_t **fields = calloc(16, sizeof(*fields));
    size_t field_lengths[16];
    uint8_t *field_data = malloc(1028);
    struct nova_tx_packet *kdescs = use_kmod
        ? calloc(height + 1, sizeof(*kdescs)) : NULL;
    if (!frame || !lines || !packets || !lengths || !fields || !field_data ||
        (use_kmod && !kdescs)) {
        fprintf(stderr, "内存分配失败\n");
        return 1;
    }

    for (int y = 0; y < height; y++) {
        lines[y] = calloc(line_len, 1);
        if (!lines[y]) return 1;
        uint8_t type = LINE_TYPE;
        make_packet(lines[y], src, type, NULL, 0);
        /* NOVA_ROW_OFF/BYTES/LE/BASE：把行号写进 43 字节包头里 34 个 0x00 中的指定位置。
         * 若诺瓦包头确实含行号字段，接收卡就能按行号定位而不依赖到达时刻。 */
        if (row_off >= 0 && row_off + row_bytes <= HEADER_LEN) {
            uint8_t *p = lines[y] + 14 + row_off;
            unsigned v = (unsigned)(y + row_base);
            if (row_bytes == 1) {
                p[0] = (uint8_t)(v & 0xFF);
            } else if (row_bytes == 2) {
                p[0] = row_le ? (uint8_t)(v & 0xFF) : (uint8_t)((v >> 8) & 0xFF);
                p[1] = row_le ? (uint8_t)((v >> 8) & 0xFF) : (uint8_t)(v & 0xFF);
            } else {
                p[0] = (uint8_t)((v >> 24) & 0xFF);
                p[1] = (uint8_t)((v >> 16) & 0xFF);
                p[2] = (uint8_t)((v >> 8) & 0xFF);
                p[3] = (uint8_t)(v & 0xFF);
            }
        }
        packets[y] = lines[y];
        lengths[y] = line_len;
    }

    for (unsigned n = 0; n < 16; n++) {
        make_field_data(n, field_data, &field_lengths[n]);
        fields[n] = calloc(15 + HEADER_LEN + field_lengths[n], 1);
        if (!fields[n]) return 1;
        make_packet(fields[n], src, FIELD_TYPE, field_data, field_lengths[n]);
    }
    free(field_data);

    fprintf(stderr, "%s: %dx%d RGB24 -> 诺瓦 %02X:%02X:%02X:%02X:%02X:%02X，目标 %.2f fps\n",
            iface, width, height, src[0], src[1], src[2], src[3], src[4], src[5], fps);
    fprintf(stderr, "每帧 %d 行包 + 1 场包，持续发送；Ctrl+C 停止后立即黑屏\n", height);

    const long long period_ns = fps > 0 ? (long long)(1000000000.0 / fps) : 0;
    /* gap=-1：自动把整帧包均匀铺满一个帧周期 */
    const long long slot_ns = (gap_us == -1 && period_ns > 0)
                                  ? period_ns / (height + 1)
                                  : 0;
    if (use_kmod) {
        long long kernel_gap_ns = gap_us > 0 ? (long long)gap_us * 1000 : slot_ns;
        kfd = open_kmod_sender(iface, height + 1, period_ns, kernel_gap_ns);
        if (kfd < 0) return 1;
    }
    /* NOVA_BATCH_SEND=1：改用 sendmmsg 整帧批量提交，由网卡按线速均匀发出 */
    const int batch_send = getenv("NOVA_BATCH_SEND") != NULL;
    struct timespec next, now, stat_last;
    clock_gettime(CLOCK_MONOTONIC, &next);
    stat_last = next;
    /* 相位错开：四台同时突发会让整屏瞬时电流叠加，错开几毫秒可减轻供电与锁存压力 */
    if (getenv("NOVA_PHASE_MS")) {
        int ms = atoi(getenv("NOVA_PHASE_MS"));
        if (ms > 0) {
            next = ts_add_ns(next, (long long)ms * 1000000LL);
            clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &next, NULL);
            clock_gettime(CLOCK_MONOTONIC, &next);
            stat_last = next;
        }
    }
    unsigned long long frame_no = 0;
    unsigned long long stat_frames = 0;
    unsigned long long kmod_dropped = 0;
    uint64_t last_kernel_frames = 0;

    while (1) {
        int rr = read_full(STDIN_FILENO, frame, frame_bytes);
        if (rr <= 0) break;

        for (int y = 0; y < height; y++) {
            memcpy(lines[y] + 15 + HEADER_LEN, frame + (size_t)y * stride, stride);
        }
        packets[height] = fields[frame_no % 16];
        lengths[height] = field_lengths[frame_no % 16];
        int snd_rc;
        if (use_kmod) {
            snd_rc = submit_kmod_frame(kfd, kdescs, packets, lengths, height + 1);
            if (snd_rc == -EAGAIN) {
                kmod_dropped++;
                snd_rc = 0;
            }
        } else if (batch_send) {
            snd_rc = send_packets_batch(fd, ifindex, packets, lengths, height + 1,
                                        gap_us > 0 ? gap_us : 17);
            if (blank_us > 0) {
                struct timespec t;
                clock_gettime(CLOCK_MONOTONIC, &t);
                t.tv_nsec += (long)blank_us * 1000L;
                while (t.tv_nsec >= 1000000000L) {
                    t.tv_nsec -= 1000000000L;
                    t.tv_sec++;
                }
                clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &t, NULL);
            }
        } else {
            snd_rc = send_packets(fd, ifindex, packets, lengths, height + 1,
                                  gap_us, blank_us, slot_ns);
        }
        if (snd_rc < 0) {
            if (fd >= 0) close(fd);
            if (kfd >= 0) close(kfd);
            return 1;
        }

        frame_no++;
        stat_frames++;
        if (fps > 0) next = ts_add_ns(next, period_ns);
        clock_gettime(CLOCK_MONOTONIC, &now);

        if (ts_diff_ns(&now, &stat_last) >= 1000000000LL) {
            double seconds = ts_diff_ns(&now, &stat_last) / 1000000000.0;
            if (use_kmod) {
                struct nova_tx_stats stats;
                if (ioctl(kfd, NOVA_TX_IOC_STATS, &stats) == 0) {
                    double kernel_fps = (stats.frames - last_kernel_frames) / seconds;
                    long long phase_us = period_ns > 0
                        ? (long long)((stats.last_frame_real_ns % (uint64_t)period_ns) / 1000)
                        : -1;
                    fprintf(stderr,
                            "内核实际 %.2f fps 丢新帧=%llu 队列满=%llu 发送错=%llu "
                            "延迟p99.9=%.0fus 最大=%.1fus 深度=%u 相位=%lldus\n",
                            kernel_fps, kmod_dropped, stats.queue_full,
                            stats.errors, kmod_late_p999(&stats),
                            stats.max_late_ns / 1000.0, stats.queue_depth, phase_us);
                    last_kernel_frames = stats.frames;
                }
            } else {
                fprintf(stderr, "实际 %.1f fps（%llu 帧）\n", stat_frames / seconds, frame_no);
            }
            stat_last = now;
            stat_frames = 0;
        }

        if (fps > 0 && !use_kmod) {
            long long late_ns = ts_diff_ns(&now, &next);
            if (late_ns > 500000000LL) {
                /* 与 Windows 当前版本一致：严重落后时重置节拍，不超速追帧 */
                next = now;
            } else if (late_ns < 0) {
                clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &next, NULL);
            }
        }
    }

    fprintf(stderr, "桌面输入已结束，共提交 %llu 帧\n", frame_no);
    if (fd >= 0) close(fd);
    if (kfd >= 0) close(kfd);
    return 0;
}
