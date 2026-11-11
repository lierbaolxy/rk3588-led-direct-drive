# -*- coding: utf-8 -*-
"""
从 S6F 抓包中提取一个完整显示帧（1080 行 + 1 个同步包）
用法: python extract_frame.py <pcapng> <out.pkl>
"""
import sys
import pickle
from scapy.utils import PcapNgReader, PcapReader


def open_reader(path):
    if path.lower().endswith((".pcapng", ".ntar")):
        return PcapNgReader(path)
    return PcapReader(path)


def main(src, dst):
    frame = []
    rows = set()
    got = 0
    first_sync = True

    with open_reader(src) as r:
        for pkt in r:
            raw = bytes(pkt)
            if len(raw) < 14:
                continue
            et = (raw[12] << 8) | raw[13]
            t = float(pkt.time)

            if (et & 0xFF00) == 0x5500:
                row = ((et & 0xFF) << 8) | raw[14]
                frame.append((t, raw))
                rows.add(row)

            elif (et & 0xFF00) == 0x0100:
                if len(rows) >= 1000:
                    frame.append((t, raw))   # 同步包放在帧尾
                    got += 1
                    if got == 1:
                        t0 = frame[0][0]
                        rel = [round((x[0] - t0) * 1000, 3) for x in frame]
                        print("完整帧: %d 个包, 行数 %d, 时长 %.3f ms" % (
                            len(frame), len(rows), rel[-1]))
                        print("同步包长度: %d, 前 48 字节: %s" % (
                            len(frame[-1][1]), frame[-1][1][:48].hex(" ")))
                        print("前 5 个包相对时间(ms):", rel[:5])
                        print("最后 5 个包相对时间(ms):", rel[-5:])
                        print("行号前 10:", [((b[1][12] << 8 | b[1][13]) & 0xFF) << 8 | b[1][14]
                                             for b in frame[:10]])
                        with open(dst, "wb") as f:
                            pickle.dump({
                                "packets": frame,
                                "rows": sorted(rows),
                                "period_ms": 1000.0 / 30.0,
                            }, f)
                        print("已保存:", dst)
                        return 0
                else:
                    frame = []
                    rows = set()

    print("没有找到完整帧")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
