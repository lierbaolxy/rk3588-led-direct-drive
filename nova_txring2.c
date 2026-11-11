/*
 * nova_txring2.c —— 诺瓦接收卡直驱发送端（TX_RING 整帧灌环版）
 *
 * 目标：复刻笔记本的行为 —— 把一整帧 1081 个包一次性灌进网卡发送环，
 * 由 MAC 按线速（约 12µs/包）背靠背发出，整帧约 13ms 发完，再等到帧周期。
 *
 * 用法：nova_txring2 <网口> <宽> <高> <fps> [gap_us]
 *   gap_us 预留，暂未实现分批间隔（整帧连发是关键）
 * 数据从 stdin 读 RGB24 帧，循环播放。
 */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <linux/if_ether.h>
#include <linux/if_packet.h>
#include <net/if.h>
#include <sched.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#define HEADER_LEN 43
#define LINE_TYPE 0x1E
#define FIELD_TYPE 0x2D

/* 环参数：帧大小 2048（够放 1498 字节包），2048 个帧位，块 4KB */
#define RING_FRAME_SIZE 2048
#define RING_FRAME_NR   2048
#define RING_BLOCK_SIZE 4096
#define RING_BLOCK_NR   (RING_FRAME_NR * RING_FRAME_SIZE / RING_BLOCK_SIZE)

static const uint8_t DST_MAC[6] = {0x12, 0x34, 0x56, 0x78, 0x9A, 0xBC};
static const uint8_t PROTO_HEADER[HEADER_LEN] = {
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0x55, 0x55, 0x55, 0x55, 0x55, 0x55, 0x55, 0xD5, 0x09
};

static void *tx_ring = NULL;
static struct sockaddr_ll g_local;   /* 绑定地址，kick 时可能要用 */

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
            perror("读取帧失败");
            return -1;
        }
    }
    return 1;
}

static size_t make_packet(uint8_t *out, const uint8_t src[6], uint8_t type) {
    memcpy(out, DST_MAC, 6);
    memcpy(out + 6, src, 6);
    out[12] = 0xFF;
    out[13] = 0x00;
    memcpy(out + 14, PROTO_HEADER, HEADER_LEN);
    out[14 + HEADER_LEN] = type;
    return 15 + HEADER_LEN;
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

static struct tpacket2_hdr *frame_hdr(int i) {
    return (struct tpacket2_hdr *)((char *)tx_ring + (size_t)i * RING_FRAME_SIZE);
}

int main(int argc, char **argv) {
    if (argc < 5) {
        fprintf(stderr, "用法: %s <网口> <宽> <高> <fps> [gap_us]\n", argv[0]);
        return 1;
    }
    const char *iface = argv[1];
    int width = atoi(argv[2]);
    int height = atoi(argv[3]);
    double fps = atof(argv[4]);

    if (width <= 0 || height <= 0) {
        fprintf(stderr, "宽高非法\n");
        return 1;
    }
    if ((size_t)height + 1 > RING_FRAME_NR) {
        fprintf(stderr, "环太小：需要 %d 帧位\n", height + 1);
        return 1;
    }

    const size_t stride = (size_t)width * 3;
    const size_t line_len = 15 + HEADER_LEN + stride;
    const size_t frame_bytes = stride * (size_t)height;

    uint8_t *frame = malloc(frame_bytes);
    if (!frame) { perror("申请帧缓冲失败"); return 1; }

    uint8_t **packets = calloc((size_t)height + 1, sizeof(uint8_t *));
    size_t *lengths = calloc((size_t)height + 1, sizeof(size_t));
    uint8_t *fields[16];
    size_t field_lengths[16];
    if (!packets || !lengths) { perror("申请包表失败"); return 1; }

    for (int y = 0; y < height; y++) {
        packets[y] = calloc(1, line_len);
        if (!packets[y]) { perror("申请行包失败"); return 1; }
        make_packet(packets[y], (const uint8_t *)"\0\0\0\0\0\0", LINE_TYPE);
        lengths[y] = line_len;
    }

    uint8_t *fd_data = malloc(1028);
    if (!fd_data) { perror("申请场包缓冲失败"); return 1; }
    for (unsigned n = 0; n < 16; n++) {
        make_field_data(n, fd_data, &field_lengths[n]);
        fields[n] = calloc(1, 15 + HEADER_LEN + field_lengths[n]);
        if (!fields[n]) { perror("申请场包失败"); return 1; }
        make_packet(fields[n], (const uint8_t *)"\0\0\0\0\0\0", FIELD_TYPE);
        memcpy(fields[n] + 15 + HEADER_LEN, fd_data, field_lengths[n]);
    }
    free(fd_data);

    /* 打开原始网口 */
    int sock = socket(AF_PACKET, SOCK_RAW, htons(ETH_P_ALL));
    if (sock < 0) { perror("创建原始网口失败（需要 root）"); return 1; }

    struct ifreq req;
    memset(&req, 0, sizeof(req));
    snprintf(req.ifr_name, IFNAMSIZ, "%s", iface);
    if (ioctl(sock, SIOCGIFHWADDR, &req) < 0) { perror("读取 MAC 失败"); return 1; }
    uint8_t src[6];
    memcpy(src, req.ifr_hwaddr.sa_data, 6);
    int ifindex = if_nametoindex(iface);
    if (!ifindex) { perror("查找网口失败"); return 1; }

    /* 把真实源 MAC 写进所有包 */
    for (int y = 0; y < height; y++) memcpy(packets[y] + 6, src, 6);
    for (unsigned n = 0; n < 16; n++) memcpy(fields[n] + 6, src, 6);

    memset(&g_local, 0, sizeof(g_local));
    g_local.sll_family = AF_PACKET;
    g_local.sll_protocol = htons(ETH_P_ALL);
    g_local.sll_ifindex = ifindex;
    if (bind(sock, (struct sockaddr *)&g_local, sizeof(g_local)) < 0) {
        perror("绑定网口失败");
        return 1;
    }

    int sndbuf = 32 * 1024 * 1024;
    setsockopt(sock, SOL_SOCKET, SO_SNDBUF, &sndbuf, sizeof(sndbuf));

    int ver = TPACKET_V2;
    if (setsockopt(sock, SOL_PACKET, PACKET_VERSION, &ver, sizeof(ver)) < 0) {
        perror("设置 TPACKET_V2 失败");
        return 1;
    }

    struct tpacket_req r;
    memset(&r, 0, sizeof(r));
    r.tp_block_size = RING_BLOCK_SIZE;
    r.tp_block_nr = RING_BLOCK_NR;
    r.tp_frame_size = RING_FRAME_SIZE;
    r.tp_frame_nr = RING_FRAME_NR;
    if (setsockopt(sock, SOL_PACKET, PACKET_TX_RING, &r, sizeof(r)) < 0) {
        perror("创建 TX_RING 失败");
        return 1;
    }

    size_t map_len = (size_t)r.tp_block_size * r.tp_block_nr;
    tx_ring = mmap(NULL, map_len, PROT_READ | PROT_WRITE, MAP_SHARED, sock, 0);
    if (tx_ring == MAP_FAILED) { perror("mmap TX_RING 失败"); return 1; }

    int one = 1;
    if (!getenv("NOVA_TXR_NOBYPASS")) {
        setsockopt(sock, SOL_PACKET, PACKET_QDISC_BYPASS, &one, sizeof(one));
    }
    int kick_mode = getenv("NOVA_TXR_KICK") ? atoi(getenv("NOVA_TXR_KICK")) : 0;

    fprintf(stderr, "%s: %dx%d RGB24 -> 诺瓦 %02X:%02X:%02X:%02X:%02X:%02X，目标 %.2f fps，TX_RING %d 帧位\n",
            iface, width, height, src[0], src[1], src[2], src[3], src[4], src[5], fps, RING_FRAME_NR);

    const long long period_ns = fps > 0 ? (long long)(1000000000.0 / fps) : 0;
    unsigned long long frame_no = 0, stat_frames = 0, stat_drains = 0;
    long long stat_ns = 0;
    struct timespec next, now, stat_last;
    clock_gettime(CLOCK_MONOTONIC, &next);
    stat_last = next;

    while (1) {
        int rr = read_full(STDIN_FILENO, frame, frame_bytes);
        if (rr <= 0) break;

        for (int y = 0; y < height; y++)
            memcpy(packets[y] + 15 + HEADER_LEN, frame + (size_t)y * stride, stride);

        const int count = height + 1;
        packets[height] = fields[frame_no % 16];
        lengths[height] = field_lengths[frame_no % 16];

        /* 整帧灌进发送环：f[0] 行包 … f[height-1] 行包，最后场包 */
        for (int i = 0; i < count; i++) {
            struct tpacket2_hdr *h = frame_hdr(i);
            int spins = 0;
            while (h->tp_status != TP_STATUS_AVAILABLE) {
                if (h->tp_status == TP_STATUS_WRONG_FORMAT) {
                    fprintf(stderr, "环帧 %d 格式错误\n", i);
                    return 1;
                }
                if (++spins > 20000000) {
                    fprintf(stderr, "环帧 %d 等待超时（status=%u）\n", i, h->tp_status);
                    return 1;
                }
                if ((spins & 0x3FF) == 0) sched_yield();
            }
            memcpy((char *)h + TPACKET2_HDRLEN, packets[i], lengths[i]);
            h->tp_len = (uint32_t)lengths[i];
            h->tp_snaplen = 0;
            __atomic_store_n(&h->tp_status, TP_STATUS_SEND_REQUEST, __ATOMIC_RELEASE);
        }

        /* 循环 kick，直到内核把这一帧全部取走（按线速 12µs/包） */
        struct timespec t_start, t_end, dbg_last;
        clock_gettime(CLOCK_MONOTONIC, &t_start);
        dbg_last = t_start;
        int done = 0, idle = 0, kicks = 0, last_err = 0;
        while (done < count) {
            ssize_t k;
            if (kick_mode == 1) {
                k = sendto(sock, NULL, 0, 0, (struct sockaddr *)&g_local, sizeof(g_local));
            } else if (kick_mode == 2) {
                k = write(sock, "", 0);
            } else {
                k = send(sock, NULL, 0, 0);
            }
            kicks++;
            if (k < 0) last_err = errno;
            if (k < 0 && (errno == EAGAIN || errno == ENOBUFS)) {
                sched_yield();
                continue;
            }
            int advanced = 0;
            while (done < count &&
                   frame_hdr(done)->tp_status == TP_STATUS_AVAILABLE) {
                done++;
                advanced = 1;
            }
            if (!advanced) {
                if (++idle > 64) { sched_yield(); idle = 0; }
            } else {
                idle = 0;
            }
            if (getenv("NOVA_TXR_DEBUG")) {
                struct timespec tdbg;
                clock_gettime(CLOCK_MONOTONIC, &tdbg);
                if (ts_diff_ns(&tdbg, &dbg_last) >= 1000000000LL) {
                    fprintf(stderr, "DBG done=%d/%d kicks=%d errno=%d st0=%u st1=%u\n",
                            done, count, kicks, last_err,
                            (unsigned)frame_hdr(0)->tp_status,
                            (unsigned)frame_hdr(1)->tp_status);
                    dbg_last = tdbg;
                    kicks = 0;
                }
            }
        }
        clock_gettime(CLOCK_MONOTONIC, &t_end);

        frame_no++;
        stat_frames++;
        stat_drains++;
        stat_ns += ts_diff_ns(&t_end, &t_start);

        if (period_ns > 0) next = ts_add_ns(next, period_ns);
        clock_gettime(CLOCK_MONOTONIC, &now);

        if (ts_diff_ns(&now, &stat_last) >= 1000000000LL) {
            double seconds = ts_diff_ns(&now, &stat_last) / 1000000000.0;
            fprintf(stderr, "实际 %.1f fps（%llu 帧） | 每帧灌发送 %.2f ms\n",
                    stat_frames / seconds, (unsigned long long)stat_frames,
                    stat_ns / stat_drains / 1000000.0);
            stat_frames = stat_drains = 0;
            stat_ns = 0;
            stat_last = now;
        }

        long long late = ts_diff_ns(&next, &now);
        if (late > 0) {
            clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &next, NULL);
        } else if (late < -500000000LL) {
            next = now;
        }
    }

    close(sock);
    return 0;
}
