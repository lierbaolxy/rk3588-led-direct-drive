#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
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
    /* 实验：NOVA_SORT_ROWS 指定排序包里每张卡的行数（默认 255） */
    int rows_env = getenv("NOVA_SORT_ROWS") ? atoi(getenv("NOVA_SORT_ROWS")) : 0;
    int order = getenv("NOVA_SORT_ORDER") ? atoi(getenv("NOVA_SORT_ORDER")) : 0;
    if (n == 0) {
        if (rows_env > 0) {
            int v = rows_env;
            int count = getenv("NOVA_SORT_N") ? atoi(getenv("NOVA_SORT_N")) : 33;
            uint8_t hi = (order == 1) ? (v & 0xFF) : ((v >> 8) & 0xFF);
            uint8_t lo = (order == 1) ? ((v >> 8) & 0xFF) : (v & 0xFF);
            data[0] = count >= 1 ? hi : 0;
            data[1] = count >= 1 ? lo : 0;
            data[2] = count >= 2 ? hi : 0;
            data[3] = count >= 2 ? lo : 0;
            data[4] = 0;
            int idx = 2;
            for (int i = 5; i < 67; i += 2) {
                if (idx >= count) {
                    data[i] = 0;
                    data[i + 1] = 0;
                } else {
                    data[i] = hi;
                    data[i + 1] = lo;
                }
                idx++;
            }
            return;
        }
        data[0] = 0x00;
        data[1] = 0xFF;
        data[2] = 0x00;
        data[3] = 0xFF;
        data[4] = 0;
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
    return fd;
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
        if (slot_ns > 0) {
            struct timespec target = ts_add_ns(base, (long long)(i + 1) * slot_ns);
            clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &target, NULL);
        } else if (gap_us > 0) {
            pkt_next = ts_add_ns(pkt_next, (long long)group * gap_us * 1000LL);
            struct timespec now;
            do {
                clock_gettime(CLOCK_MONOTONIC, &now);
            } while (ts_diff_ns(&pkt_next, &now) > 0);
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

    uint8_t src[6];
    int ifindex;
    int fd = open_sender(iface, src, &ifindex);
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
        uint8_t type = LINE_TYPE;
        make_packet(lines[y], src, type, NULL, 0);
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
    int field_mode = 0;
    if (getenv("NOVA_FIELD_MODE")) field_mode = atoi(getenv("NOVA_FIELD_MODE"));
    /* gap=-1：自动把整帧包均匀铺满一个帧周期 */
    const long long slot_ns = (gap_us == -1 && period_ns > 0)
                                  ? period_ns / (height + 1)
                                  : 0;
    struct timespec next, now, stat_last;
    clock_gettime(CLOCK_MONOTONIC, &next);
    stat_last = next;
    unsigned long long frame_no = 0;
    unsigned long long stat_frames = 0;

    while (1) {
        int rr = read_full(STDIN_FILENO, frame, frame_bytes);
        if (rr <= 0) break;

        for (int y = 0; y < height; y++) {
            memcpy(lines[y] + 15 + HEADER_LEN, frame + (size_t)y * stride, stride);
        }
        int extra = 0;
        if (field_mode == 0) {
            packets[height] = fields[frame_no % 16];
            lengths[height] = field_lengths[frame_no % 16];
            extra = 1;
        } else if (field_mode == 1) {
            packets[height] = fields[0];
            lengths[height] = field_lengths[0];
            extra = 1;
        } else if (field_mode == 2) {
            packets[height] = fields[1];
            lengths[height] = field_lengths[1];
            extra = 1;
        }
        if (send_packets(fd, ifindex, packets, lengths, height + extra, gap_us, blank_us, slot_ns) < 0) {
            close(fd);
            return 1;
        }

        frame_no++;
        stat_frames++;
        if (fps > 0) next = ts_add_ns(next, period_ns);
        clock_gettime(CLOCK_MONOTONIC, &now);

        if (ts_diff_ns(&now, &stat_last) >= 1000000000LL) {
            double seconds = ts_diff_ns(&now, &stat_last) / 1000000000.0;
            fprintf(stderr, "实际 %.1f fps（%llu 帧）\n", stat_frames / seconds, frame_no);
            stat_last = now;
            stat_frames = 0;
        }

        if (fps > 0) {
            long long late_ns = ts_diff_ns(&now, &next);
            if (late_ns > 500000000LL) {
                /* 与 Windows 当前版本一致：严重落后时重置节拍，不超速追帧 */
                next = now;
            } else if (late_ns < 0) {
                clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &next, NULL);
            }
        }
    }

    fprintf(stderr, "桌面输入已结束，共发送 %llu 帧\n", frame_no);
    close(fd);
    return 0;
}
