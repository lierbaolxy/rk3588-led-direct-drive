# -*- coding: utf-8 -*-
"""
分析同步包 (EtherType 0x01xx) 中逐帧变化的字节
用法: python analyze_sync.py <pcapng>
"""
import sys
from scapy.utils import PcapNgReader, PcapReader


def open_reader(path):
    if path.lower().endswith((".pcapng", ".ntar")):
        return PcapNgReader(path)
    return PcapReader(path)


def main(path):
    syncs = []
    with open_reader(path) as r:
        for pkt in r:
            raw = bytes(pkt)
            if len(raw) < 14:
                continue
            et = (raw[12] << 8) | raw[13]
            if (et & 0xFF00) == 0x0100 and len(raw) > 1000:
                syncs.append(raw)
                if len(syncs) >= 20:
                    break

    print("同步包数量: %d, 长度: %s" % (len(syncs), [len(x) for x in syncs][:3]))
    if len(syncs) < 2:
        return

    n = min(len(x) for x in syncs)
    varying = []
    for i in range(n):
        vals = {x[i] for x in syncs}
        if len(vals) > 1:
            varying.append((i, sorted(vals)))

    print("变化字节位置数: %d" % len(varying))
    for i, vals in varying[:12]:
        seq = [x[i] for x in syncs[:12]]
        print("  offset %4d (data[%d]): %s" % (i, i - 13, seq))

    print("\n前 3 个同步包前 48 字节:")
    for x in syncs[:3]:
        print("  " + x[:48].hex(" "))


if __name__ == "__main__":
    main(sys.argv[1])
