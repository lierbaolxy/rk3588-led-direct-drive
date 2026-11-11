#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""诺瓦接收卡持续点亮工具：预构建 16 个发送队列，按目标 fps 循环发送。

用法:
    python nova_live.py --pattern calib --fps 60
    python nova_live.py --pattern bands3 --fps 30
停止:
    新建 .stop_send 文件，或用 Ctrl+C
"""
import argparse
import ctypes
import os
import sys
import time

WPCAP = r"C:\Windows\System32\Npcap\wpcap.dll"
IFACE = r"\Device\NPF_{579A70D0-DE8A-4C4D-B93F-B168A4E9E5FF}"
DST_MAC = bytes([0x12, 0x34, 0x56, 0x78, 0x9A, 0xBC])
HEADER = bytes([0] * 34 + [0x55] * 7 + [0xD5, 0x09])
LINE_TYPE = 0x1E
FIELD_TYPE = 0x2D
HERE = os.path.dirname(os.path.abspath(__file__))
STOP_FILE = os.path.join(HERE, ".stop_send")


class pkthdr(ctypes.Structure):
    _fields_ = [("ts_s", ctypes.c_long), ("ts_u", ctypes.c_long),
                ("caplen", ctypes.c_uint32), ("len", ctypes.c_uint32)]


def make_field_data(n):
    if n == 0:
        d = bytearray(1028)
        d[0], d[1], d[2], d[3] = 0x00, 0xFF, 0x00, 0xFF
        i = 5
        while i < 67:
            d[i] = 0x00
            d[i + 1] = 0xFF
            i += 2
        return bytes(d)
    if n == 1:
        d = bytearray(538)
        d[0:4] = b"\x01\xfe\x00\xff"
        return bytes(d)
    if n < 8:
        d = bytearray(1028)
        d[0] = n & 0xFF
        d[1] = (n >> 8) & 0xFF
        d[2] = 0x00
        d[3] = 0xFF
        return bytes(d)
    return bytes(1028)


def make_pattern(name, width, height):
    stride = width * 3
    buf = bytearray(stride * height)

    def bands(colors):
        n = len(colors)
        for y in range(height):
            b, g, r = colors[min(y * n // height, n - 1)]
            buf[y * stride:(y + 1) * stride] = bytes([b, g, r]) * width

    if name == "calib":
        bands([(0, 0, 255), (0, 255, 0), (255, 0, 0),
               (255, 255, 255), (0, 255, 255), (255, 255, 0)])
    elif name == "bands3":
        bands([(0, 0, 255), (0, 255, 0), (255, 0, 0)])
    elif name in ("white", "red", "green", "blue", "black"):
        bgr = {"white": (255, 255, 255), "red": (0, 0, 255), "green": (0, 255, 0),
               "blue": (255, 0, 0), "black": (0, 0, 0)}[name]
        buf = bytearray(bytes(bgr) * width * height)
    elif name == "gray-v":
        for y in range(height):
            v = y * 255 // max(1, height - 1)
            buf[y * stride:(y + 1) * stride] = bytes([v, v, v]) * width
    elif name == "gray-h":
        for y in range(height):
            row = bytearray()
            for x in range(width):
                v = x * 255 // max(1, width - 1)
                row += bytes([v, v, v])
            buf[y * stride:(y + 1) * stride] = row
    else:
        raise SystemExit("unknown pattern: " + name)
    return bytes(buf)


def load_pcap():
    lib = ctypes.WinDLL(WPCAP, winmode=0x8)
    lib.pcap_open_live.restype = ctypes.c_void_p
    lib.pcap_open_live.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_char_p]
    lib.pcap_sendqueue_alloc.restype = ctypes.c_void_p
    lib.pcap_sendqueue_alloc.argtypes = [ctypes.c_uint]
    lib.pcap_sendqueue_queue.restype = ctypes.c_int
    lib.pcap_sendqueue_queue.argtypes = [ctypes.c_void_p, ctypes.POINTER(pkthdr),
                                         ctypes.c_char_p]
    lib.pcap_sendqueue_transmit.restype = ctypes.c_int
    lib.pcap_sendqueue_transmit.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
    lib.pcap_sendqueue_destroy.argtypes = [ctypes.c_void_p]
    lib.pcap_geterr.restype = ctypes.c_char_p
    lib.pcap_geterr.argtypes = [ctypes.c_void_p]
    lib.pcap_close.argtypes = [ctypes.c_void_p]
    return lib


def open_iface(lib, iface, retries=20):
    err = ctypes.create_string_buffer(256)
    for attempt in range(retries):
        h = lib.pcap_open_live(iface.encode(), 65536, 1, 10, err)
        if h:
            return h
        msg = err.value.decode("ascii", errors="replace")[:80]
        print(f"  打开网卡重试 {attempt + 1}/{retries}: {msg}", flush=True)
        time.sleep(1)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--iface", default=IFACE)
    ap.add_argument("--srcmac", default="10:b6:76:42:42:8e")
    ap.add_argument("--width", type=int, default=480)
    ap.add_argument("--height", type=int, default=1080)
    ap.add_argument("--fps", type=float, default=60)
    ap.add_argument("--pattern", default="calib")
    ap.add_argument("--seconds", type=float, default=0, help="0=一直发")
    ap.add_argument("--gap", type=int, default=-1,
                    help="-1=整帧均匀铺满帧周期；0=瞬时突发")
    a = ap.parse_args()

    if os.path.exists(STOP_FILE):
        os.remove(STOP_FILE)

    src = bytes(int(x, 16) for x in a.srcmac.split(":"))
    w, ht = a.width, a.height
    stride = w * 3
    period_us = int(round(1_000_000.0 / a.fps))
    n_pkt = ht + 1
    if a.gap < 0:
        gap_us = max(1, int(round(period_us / n_pkt)))
    else:
        gap_us = a.gap

    print(f"[1/4] 加载 Npcap...", flush=True)
    lib = load_pcap()
    print(f"[2/4] 打开网卡 {a.iface}", flush=True)
    h = open_iface(lib, a.iface)
    if not h:
        print("打开网卡失败", flush=True)
        return 1

    print(f"[3/4] 构建 {a.width}x{a.height} 图案 '{a.pattern}' 与 16 个队列...", flush=True)
    img = make_pattern(a.pattern, w, ht)

    queues = []
    alloc_ok = True
    for n in range(16):
        field = DST_MAC + src + b"\xff\x00" + HEADER + bytes([FIELD_TYPE]) + make_field_data(n)
        pkts = []
        for y in range(ht):
            pkts.append(DST_MAC + src + b"\xff\x00" + HEADER + bytes([LINE_TYPE])
                        + img[y * stride:(y + 1) * stride])
        pkts.append(field)
        q = lib.pcap_sendqueue_alloc(sum(len(p) + 512 for p in pkts) + 4096)
        if not q:
            print("  队列分配失败", flush=True)
            alloc_ok = False
            break
        for i, p in enumerate(pkts):
            hd = pkthdr()
            hd.ts_s = 0
            hd.ts_u = i * gap_us
            hd.caplen = len(p)
            hd.len = len(p)
            if lib.pcap_sendqueue_queue(q, ctypes.byref(hd), p) != 0:
                alloc_ok = False
                print(f"  入队失败 n={n} i={i}", flush=True)
                break
        queues.append(q)
        if not alloc_ok:
            break

    if not alloc_ok:
        for q in queues:
            lib.pcap_sendqueue_destroy(q)
        lib.pcap_close(h)
        return 1

    frame_mb = (stride * ht) / 1e6
    mbps = frame_mb * 8 * a.fps
    print(f"[4/4] 开始发送  fps={a.fps}  每帧 {n_pkt} 包 / {frame_mb:.2f} MB  "
          f"理论 {mbps:.0f} Mbps  包间隔 {gap_us}us", flush=True)
    print("      Ctrl+C 或新建 .stop_send 文件停止", flush=True)

    sent = 0
    t_start = time.perf_counter()
    next_t = t_start
    last_report = t_start
    last_frames = 0
    worst = 0.0
    slow = 0
    use_sync = 1 if a.gap < 0 else 0

    try:
        while True:
            if os.path.exists(STOP_FILE):
                print("检测到 .stop_send，正常退出", flush=True)
                break
            q = queues[sent % 16]
            t0 = time.perf_counter()
            rc = lib.pcap_sendqueue_transmit(h, q, use_sync)
            dt = time.perf_counter() - t0
            if dt > worst:
                worst = dt
            if rc <= 0:
                err = lib.pcap_geterr(h)
                print("发送失败:", err.decode(errors="ignore") if err else "", flush=True)
                break
            sent += 1

            now = time.perf_counter()
            if a.seconds > 0 and now - t_start >= a.seconds:
                break

            if now - last_report >= 1.0:
                fps_now = (sent - last_frames) / (now - last_report)
                if fps_now < a.fps * 0.95:
                    slow += 1
                print(f"  实测 {fps_now:.2f} fps  累计 {sent} 帧  "
                      f"单帧耗时 当前 {dt * 1000:.2f}ms 最长 {worst * 1000:.2f}ms", flush=True)
                last_report = now
                last_frames = sent
                worst = 0.0

            # 节拍：按绝对时刻排程，落后超过半帧就重置基准（不追帧）
            if a.gap < 0:
                dt_target = 1.0 / a.fps
                next_t += dt_target
                if now - next_t > dt_target * 0.5:
                    next_t = now
                elif next_t > now:
                    time.sleep(next_t - now)
            else:
                nxt = t_start + sent / a.fps
                if now - nxt > 0.5:
                    t_start = now - sent / a.fps
                elif nxt > now:
                    time.sleep(nxt - now)
    except KeyboardInterrupt:
        print("\n中断", flush=True)
    finally:
        for q in queues:
            lib.pcap_sendqueue_destroy(q)
        lib.pcap_close(h)

    total = time.perf_counter() - t_start
    print(f"结束：{sent} 帧 / {total:.1f}s = {sent / max(0.001, total):.2f} fps  "
          f"低于目标 {slow} 次", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
