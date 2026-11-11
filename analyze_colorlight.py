# -*- coding: utf-8 -*-
"""
卡莱特 S6F -> 接收卡 抓包分析
用法: python analyze_colorlight.py <pcapng>
"""
import sys
import collections
from scapy.utils import PcapNgReader, PcapReader

DST_MAC = "11:22:33:44:55:66"
SRC_MAC = "22:22:33:44:55:66"


def mac(b):
    return ":".join("%02x" % x for x in b)


def open_reader(path):
    if path.lower().endswith((".pcapng", ".ntar")):
        return PcapNgReader(path)
    return PcapReader(path)


def main(path):
    counts = collections.Counter()
    lengths = collections.defaultdict(list)
    macs = collections.Counter()
    first_hex = {}
    times = []
    rows = collections.Counter()
    row_seq = []
    sync_info = []
    resp_info = []
    pixel_samples = []
    last_t = None
    gaps = collections.Counter()

    with open_reader(path) as r:
        for pkt in r:
            raw = bytes(pkt)
            if len(raw) < 14:
                continue
            t = float(getattr(pkt, "time", 0) or 0)
            times.append(t)
            if last_t is not None:
                d = t - last_t
                if d > 0:
                    gaps[round(d * 1000, 3)] += 1
            last_t = t

            et = (raw[12] << 8) | raw[13]
            counts[et] += 1
            lengths[et].append(len(raw))
            macs[(mac(raw[0:6]), mac(raw[6:12]))] += 1
            if et not in first_hex:
                first_hex[et] = raw[:96].hex(" ")

            if (et & 0xFF00) == 0x5500:
                # data[0] 在 EtherType 低字节
                data0 = et & 0xFF
                row = (data0 << 8) | raw[14]
                off = (raw[15] << 8) | raw[16]
                cnt = (raw[17] << 8) | raw[18]
                marker6 = raw[19]
                marker7 = raw[20]
                rows[row] += 1
                if len(row_seq) < 40:
                    row_seq.append(row)
                if len(pixel_samples) < 5:
                    pixel_samples.append((row, off, cnt, marker6, marker7, len(raw), raw[21:30].hex(" ")))

            elif et == 0x0700:
                resp_info.append(("discover", raw[:40].hex(" ")))

            elif (et & 0xFF00) == 0x0800:
                data = raw[13:]
                resp_info.append((
                    "resp",
                    len(raw),
                    data[0], data[1], data[2], data[3],
                    (data[21] << 8) | data[22],
                    (data[23] << 8) | data[24],
                    data[85] if len(data) > 85 else None,
                ))

            elif (et & 0xFF00) == 0x0100:
                data = raw[13:]
                sync_info.append((len(raw), data[0], data[22] if len(data) > 22 else None,
                                  data[23] if len(data) > 23 else None))

    total = sum(counts.values())
    dur = (max(times) - min(times)) if len(times) > 1 else 0
    print("文件: %s" % path)
    print("总帧数: %d" % total)
    print("时间跨度: %.3f s" % dur)
    if dur > 0:
        print("平均速率: %.0f 帧/s" % (total / dur))
    print("\n== EtherType 分布 ==")
    for et, n in counts.most_common(20):
        ls = lengths[et]
        print("  0x%04x  %8d 帧  %6.1f%%  帧长 %d..%d 平均 %.0f" % (
            et, n, 100.0 * n / total, min(ls), max(ls), sum(ls) / len(ls)))

    print("\n== MAC 对分布 ==")
    for (d, s), n in macs.most_common(6):
        tag = ""
        if d.upper() == DST_MAC and s.upper() == SRC_MAC:
            tag = "  <- 发送卡发往接收卡"
        elif d.upper() == "FF:FF:FF:FF:FF:FF":
            tag = "  <- 接收卡广播回复"
        print("  dst=%s src=%s  %d%s" % (d, s, n, tag))

    print("\n== 像素行包 (0x55xx) ==")
    print("  不同行号数量: %d" % len(rows))
    if rows:
        print("  行号范围: %d .. %d" % (min(rows), max(rows)))
        print("  每行包数(前10): %s" % [(k, rows[k]) for k in sorted(rows)[:10]])
        print("  行号序列(前40): %s" % row_seq)
        print("  样包(row, off, cnt, b6, b7, len, pixels):")
        for s in pixel_samples:
            print("    %s" % (s,))

    print("\n== 同步包 (0x01xx) ==")
    print("  数量: %d" % len(sync_info))
    for s in sync_info[:5]:
        print("    len=%d data0=0x%02x data22=%s data23=%s" % s)

    print("\n== 发现/回复包 ==")
    print("  数量: %d" % len(resp_info))
    for s in resp_info[:8]:
        print("    %s" % (s,))

    print("\n== 各类型首包前 96 字节 ==")
    for et in sorted(first_hex):
        print("  0x%04x: %s" % (et, first_hex[et]))

    print("\n== 帧间隔最多的值(ms) ==")
    for g, n in gaps.most_common(8):
        print("  %s ms : %d" % (g, n))


if __name__ == "__main__":
    main(sys.argv[1])
