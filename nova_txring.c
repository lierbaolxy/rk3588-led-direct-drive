/*
 * nova_txring.c —— 诺瓦接收卡直驱发送端（PACKET_TX_RING 版）
 *
 * 为什么需要这一版：
 *   逐包 sendto 每次系统调用 20-50µs，而 50fps 要求 17µs/包、30fps 要求 30.8µs/包，
 *   用户态循环根本追不上；整帧 sendmmsg 虽快，但驱动 ring 满时会重试，
 *   实测发包计数比理论多 30%，接收卡收到重复行 → 画面错位。
 *
 * PACKET_TX_RING 把发送环形缓冲 mmap 到用户空间：
 *   程序只写描述符（零系统调用），内核按线速取包，每帧只发一次、顺序确定。
 *   一帧 1081 包按线速约 13ms 发完，与 V960 自身的突发节奏一致。
 *
 * 用法完全相同：
 *   nova_txring <网口> [宽] [高] [fps] [gap_us] [blank_us]
 *   ffmpeg ... -f rawvideo -pix_fmt rgb24 - | nova_txring eth0 480 1080 30 0 0
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
#define RING_FRAMES 4096
#define RING_FRAME_SIZE 4096
#define RING_BLOCKS 4096

static const uint8_t DST_MAC[6] = {0x12, 0x34, 0x56, 0x78, 0x9A, 0xBC};
static const uint8_t PROTO_HEADER[HEADER_LEN] = {
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0x55, 0x55, 0x55, 0x55, 0x55, 0x55, 0x55, 0xD5, 0x09
};

static void *tx_ring = NULL;
static size_t ring_map_len = 0;

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

static int open_sender(const char *iface, uint8_t src[6], int *ifindex,
                       int use_txring) {
    int fd = socket(AF_PACKET, SOCK_RAW, htons(ETH_P_ALL));
    if (fd < 0) {
        perror("创建原始网口失败（需要 root）");
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

    if (!use_txring) return fd;

    /* 版本必须显式指定 TPACKET_V2，否则 tp_status 语义不一致 */
    int ver = TPACKET_V2;
    if (setsockopt(fd, SOL_PACKET, PACKET_VERSION, &ver, sizeof(ver)) < 0) {
        perror("设置 TPACKET_V2 失败");
        close(fd);
        return -1;
    }

    /* 一块一帧，简化下标计算：第 i 帧地址 = ring + i * RING_FRAME_SIZE */
    struct tpacket_req r;
    memset(&r, 0, sizeof(r));
    r.tp_block_size = RING_FRAME_SIZE;
    r.tp_block_nr = RING_BLOCKS;
    r.tp_frame_size = RING_FRAME_SIZE;
    r.tp_frame_nr = RING_FRAMES;
    if (setsockopt(fd, SOL_PACKET, PACKET_TX_RING, &r, sizeof(r)) < 0) {
        perror("创建 TX_RING 失败");
        close(fd);
        return -1;
    }

    ring_map_len = (size_t)r.tp_block_size * r.tp_block_nr;
    tx_ring = mmap(NULL, ring_map_len, PROT_READ | PROT_WRITE,
                   MAP_SHARED | MAP_LOCKED, fd, 0);
    if (tx_ring == MAP_FAILED) {
        /* MAP_LOCKED 在部分内核/权限下会失败，退回普通映射 */
        tx_ring = mmap(NULL, ring_map_len, PROT_READ | PROT_WRITE,
                       MAP_SHARED, fd, 0);
    }
    if (tx_ring == MAP_FAILED) {
        perror("mmap TX_RING 失败");
        tx_ring = NULL;
        close(fd);
        return -1;
    }

    /* 绕过 qdisc：包直接进驱动 ring，不受 tc 队列调度影响 */
    int one = 1;
    setsockopt(fd, SOL_PACKET, PACKET_QDISC_BYPASS, &one, sizeof(one));

    /* 初始状态：全部可用 */
    for (int i = 0; i < RING_FRAMES; i++) {
        struct tpacket2_hdr *h = (struct tpacket2_hdr *)
            ((char *)tx_ring + (size_t)i * RING_FRAME_SIZE);
        h->tp_status = TP_STATUS_AVAILABLE;
    }
    return fd;
}

/* 把整帧包写进环并触发发送，等到全部真正发完才返回。
 * 每帧只提交一次，不存在重复发送。 */
static int txring_send_frame(int fd, uint8_t **packets, size_t *lengths,
                             int count, int *next_slot, int gap_us) {
    int slot = *next_slot;
    struct timespec t0, now, target;
    clock_gettime(CLOCK_MONOTONIC, &t0);

    for (int i = 0; i < count; i++) {
        int idx = slot + i;
        if (idx >= RING_FRAMES) idx -= RING_FRAMES;
        struct tpacket2_hdr *h = (struct tpacket2_hdr *)
            ((char *)tx_ring + (size_t)idx * RING_FRAME_SIZE);
        if (h->tp_status != TP_STATUS_AVAILABLE) {
            fprintf(stderr, "环帧 %d 未释放（status=%u）\n", idx, h->tp_status);
            return -1;
        }
        memcpy((char *)h + TPACKET2_HDRLEN, packets[i], lengths[i]);
        h->tp_len = lengths[i];
        h->tp_snaplen = 0;
        __sync_synchronize();
        h->tp_status = TP_STATUS_SEND_REQUEST;

        /* 每 4 包触发一次发送，再按 gap_us 等待：
         * 若整帧一次性提交，内核按线速 12µs/包连发，接收卡内部行缓冲被冲掉，
         * 只剩最后几行能显示（表现为屏上只有底部一条）。
         * 分批提交 + 等待可把包间隔拉到 18~30µs，与笔记本实测档位一致。 */
        if ((i + 1) % 4 == 0 || i == count - 1) {
            if (send(fd, NULL, 0, 0) < 0) {
                if (errno != EINTR && errno != EAGAIN) {
                    perror("触发 TX_RING 发送失败");
                    return -1;
                }
            }
            if (gap_us > 0) {
                target = ts_add_ns(t0, (long long)(i + 1) * gap_us * 1000LL);
                do {
                    clock_gettime(CLOCK_MONOTONIC, &now);
                } while (ts_diff_ns(&target, &now) > 0);
            }
        }
    }

    /* 等内核把这一帧的包全部取走（回到 AVAILABLE） */
    for (int i = 0; i < count; i++) {
        int idx = slot + i;
        if (idx >= RING_FRAMES) idx -= RING_FRAMES;
        struct tpacket2_hdr *h = (struct tpacket2_hdr *)
            ((char *)tx_ring + (size_t)idx * RING_FRAME_SIZE);
        int spins = 0;
        while (h->tp_status != TP_STATUS_AVAILABLE) {
            if (h->tp_status == TP_STATUS_WRONG_FORMAT) {
                fprintf(stderr, "环帧 %d 格式错误\n", idx);
                return -1;
            }
            __sync_synchronize();
            if (++spins > 20000000) {
                fprintf(stderr, "环帧 %d 等待超时\n", idx);
                return -1;
            }
            if ((spins & 0xFF) == 0) sched_yield();
        }
    }

    *next_slot = slot + count;
    if (*next_slot >= RING_FRAMES) *next_slot -= RING_FRAMES;
    return 0;
}

int main(int argc, char **argv) {
    if (argc < 2 || argc > 7) {
        fprintf(stderr, "用法: %s <网口> [宽=480] [高=1080] [fps=60] [gap_us=0] [场消隐us=0]\n", argv[0]);
        fprintf(stderr, "从标准输入读 RGB24，例如 ffmpeg ... -pix_fmt rgb24 -f rawvideo - | %s eth0\n", argv[0]);
        return 2;
    }

    const char *iface = argv[1];
    int width = argc > 2 ? atoi(argv[2]) : 480;
    int height = argc > 3 ? atoi(argv[3]) : 1080;
    double fps = argc > 4 ? atof(argv[4]) : 60.0;
    int gap_us = argc > 5 ? atoi(argv[5]) : 0;
    int blank_us = argc > 6 ? atoi(argv[6]) : 0;
    if (width <= 0 || height <= 0 || fps < 0 || width > 4096 || height > 4096) {
        fprintf(stderr, "参数无效\n");
        return 2;
    }
    if (height + 1 > RING_FRAMES) {
        fprintf(stderr, "高度 %d 超过环形缓冲 %d 帧\n", height + 1, RING_FRAMES);
        return 2;
    }

    uint8_t src[6];
    int ifindex;
    int fd = open_sender(iface, src, &ifindex, 1);
    if (fd < 0) return 1;

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
    if (!frame || !lines || !packets || !lengths || !fields || !field_data) {
        fprintf(stderr, "内存分配失败\n");
        return 1;
    }

    for (int y = 0; y < height; y++) {
        lines[y] = calloc(line_len, 1);
        if (!lines[y]) return 1;
        make_packet(lines[y], src, LINE_TYPE, NULL, 0);
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

    fprintf(stderr, "%s: %dx%d RGB24 -> 诺瓦 %02X:%02X:%02X:%02X:%02X:%02X，目标 %.2f fps，TX_RING %d 帧\n",
            iface, width, height, src[0], src[1], src[2], src[3], src[4], src[5],
            fps, RING_FRAMES);
    fprintf(stderr, "每帧 %d 行包 + 1 场包，由内核按线速连续发出\n", height);

    const long long period_ns = fps > 0 ? (long long)(1000000000.0 / fps) : 0;
    struct timespec next, now, stat_last;
    clock_gettime(CLOCK_MONOTONIC, &next);
    stat_last = next;
    unsigned long long frame_no = 0, stat_frames = 0;
    int slot = 0;

    while (1) {
        int rr = read_full(STDIN_FILENO, frame, frame_bytes);
        if (rr <= 0) break;

        for (int y = 0; y < height; y++) {
            memcpy(lines[y] + 15 + HEADER_LEN, frame + (size_t)y * stride, stride);
        }
        packets[height] = fields[frame_no % 16];
        lengths[height] = field_lengths[frame_no % 16];

        if (txring_send_frame(fd, packets, lengths, height + 1, &slot,
                              gap_us > 0 ? gap_us : 20) < 0) {
            close(fd);
            return 1;
        }

        frame_no++;
        stat_frames++;
        if (fps > 0) next = ts_add_ns(next, period_ns);
        clock_gettime(CLOCK_MONOTONIC, &now);

        if (ts_diff_ns(&now, &stat_last) >= 1000000000LL) {
            double sec = ts_diff_ns(&now, &stat_last) / 1000000000.0;
            fprintf(stderr, "实际 %.1f fps（%llu 帧）\n", stat_frames / sec, frame_no);
            stat_last = now;
            stat_frames = 0;
        }

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

        if (fps > 0) {
            long long late = ts_diff_ns(&now, &next);
            if (late > 500000000LL) {
                next = now;
            } else if (late < 0) {
                clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &next, NULL);
            }
        }
    }

    fprintf(stderr, "输入结束，共发送 %llu 帧\n", frame_no);
    close(fd);
    return 0;
}
