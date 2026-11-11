#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
诺瓦接收卡直驱发送程序（严格按 olebeck/novaled 协议，1:1 移植）
  以太网   : dst=12:34:56:78:9A:BC  ethertype=0xFF00
  包头     : 43 字节 = 34*0x00 + 7*0x55 + 0xD5 + 0x09
  行包     : 包头 + 0x1E + 该行像素(BGR)，无行号，按发送顺序对应行
  场包     : 包头 + 0x2D + 1028 字节，16 帧循环
  每帧顺序 : 行0..行H-1，最后 1 个场包
"""
import argparse
import ctypes
import os
import queue
import subprocess
import sys
import threading
import time

WPCAP = r"C:\Windows\System32\Npcap\wpcap.dll"
# 出现这个文件时发送进程正常退出，避免强杀导致 Npcap 网卡句柄长时间被占用
STOP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".stop_send")
DST_MAC = bytes([0x12, 0x34, 0x56, 0x78, 0x9A, 0xBC])
HEADER = bytes([0] * 34 + [0x55] * 7 + [0xD5, 0x09])
LINE_TYPE = 0x1E
FIELD_TYPE = 0x2D


def _sort_field():
    # 实测 V960：n=0 场包 = 00 FF 00 FF… 共 33 个 0xFF（与 novaled 一致）
    d = bytearray(1028)
    d[0], d[1], d[2], d[3] = 0x00, 0xFF, 0x00, 0xFF
    i = 5
    while i < 67:
        d[i] = 0x00
        d[i + 1] = 0xFF
        i += 2
    return bytes(d)


def _param_field():
    d = bytearray(538)
    d[0:4] = b"\x01\xfe\x00\xff"
    return bytes(d)


def _reserve_field(n):
    d = bytearray(1028)
    d[0] = n & 0xFF
    d[1] = (n >> 8) & 0xFF
    d[2] = 0x00
    d[3] = 0xFF
    return bytes(d)


def get_field_packet(frame_no):
    n = frame_no % 16
    if n == 0:
        return _sort_field()
    if n == 1:
        return _param_field()
    if n < 8:
        return _reserve_field(n)
    return bytes(1028)


def payload(frametype, data):
    return HEADER + bytes([frametype]) + data


def eth(p, src):
    return DST_MAC + src + b"\xff\x00" + p


def make_pattern(name, width, height):
    stride = width * 3
    if name == "bands3":
        # 三个箱体分界（360 行一段）：上红、中绿、下蓝
        # 用来判断哪个箱体收到了哪一段数据
        bands = [(0, 0, 255), (0, 255, 0), (255, 0, 0)]
        buf = bytearray(stride * height)
        n = len(bands)
        for y in range(height):
            b, g, r = bands[min(y * n // height, n - 1)]
            buf[y * stride:(y + 1) * stride] = bytes([b, g, r]) * width
        return buf
    if name == "calib":
        bands = [(0, 0, 255), (0, 255, 0), (255, 0, 0),
                 (255, 255, 255), (0, 255, 255), (255, 255, 0)]
        buf = bytearray(stride * height)
        n = len(bands)
        for y in range(height):
            b, g, r = bands[min(y * n // height, n - 1)]
            buf[y * stride:(y + 1) * stride] = bytes([b, g, r]) * width
        return buf
    color = {"white": (255, 255, 255), "red": (0, 0, 255), "green": (0, 255, 0),
             "blue": (255, 0, 0), "black": (0, 0, 0)}.get(name)
    if color:
        return bytearray(bytes(color) * width * height)
    if name == "gray-v":
        buf = bytearray(stride * height)
        for y in range(height):
            v = y * 255 // max(1, height - 1)
            buf[y * stride:(y + 1) * stride] = bytes([v, v, v]) * width
        return buf
    if name == "gray-h":
        buf = bytearray(stride * height)
        for y in range(height):
            row = bytearray()
            for x in range(width):
                v = x * 255 // max(1, width - 1)
                row += bytes([v, v, v])
            buf[y * stride:(y + 1) * stride] = row
        return buf
    raise SystemExit("unknown pattern: " + name)


def read_exact(stream, size):
    parts = []
    while size:
        data = stream.read(size)
        if not data:
            return None
        parts.append(data)
        size -= len(data)
    return b"".join(parts)


class timeval(ctypes.Structure):
    _fields_ = [("s", ctypes.c_long), ("u", ctypes.c_long)]


class pkthdr(ctypes.Structure):
    _fields_ = [("ts", timeval), ("caplen", ctypes.c_uint32), ("len", ctypes.c_uint32)]


def run_video(lib, h, a, w, ht, stride, frame_bytes, gap_us, spawn_video, fields, src,
              swap=False):
    """视频分块流水发送：解码与建包都在后台线程，主线程只做发送，避免 60fps 抖动"""
    import numpy as np

    pkt_len = 58 + stride
    buf = np.zeros((ht + 1, pkt_len), dtype=np.uint8)
    buf[:ht, :58] = np.frombuffer(eth(payload(LINE_TYPE, b""), src), dtype=np.uint8)
    field_arr = [np.frombuffer(x, dtype=np.uint8) for x in fields]
    field_len = [len(x) for x in fields]
    base = buf.ctypes.data
    cptrs = [ctypes.cast(ctypes.cast(base + i * pkt_len, ctypes.POINTER(ctypes.c_ubyte)),
                         ctypes.c_char_p)
             for i in range(ht + 1)]

    period_us = int(round(1_000_000.0 / a.fps))
    chunk = max(1, a.chunk)
    hdrs = [[None] * (ht + 1) for _ in range(chunk)]
    brefs = [[None] * (ht + 1) for _ in range(chunk)]
    for f in range(chunk):
        for i in range(ht + 1):
            hd = pkthdr()
            hd.ts.s = 0
            hd.ts.u = f * period_us + i * gap_us
            hd.caplen = pkt_len
            hd.len = pkt_len
            hdrs[f][i] = hd
            brefs[f][i] = ctypes.byref(hd)

    stop_ev = threading.Event()
    fr_q = queue.Queue(maxsize=12)
    ck_q = queue.Queue(maxsize=2)
    counter = [0]

    def decode_loop(proc):
        while not stop_ev.is_set():
            data = read_exact(proc.stdout, frame_bytes)
            if data is None:
                try:
                    proc.stdout.close()
                except OSError:
                    pass
                proc.wait()
                proc = spawn_video()
                continue
            while not stop_ev.is_set():
                try:
                    fr_q.put(data, timeout=0.5)
                    break
                except queue.Full:
                    continue
        try:
            proc.terminate()
        except OSError:
            pass

    def build_loop():
        while not stop_ev.is_set():
            frames = []
            while len(frames) < chunk:
                try:
                    frames.append(fr_q.get(timeout=0.5))
                except queue.Empty:
                    if frames:
                        break
            if not frames:
                continue
            q = lib.pcap_sendqueue_alloc(len(frames) * (ht + 1) * (pkt_len + 512))
            if not q:
                print("队列分配失败")
                stop_ev.set()
                return
            for f, data in enumerate(frames):
                arr = np.frombuffer(data, dtype=np.uint8).reshape(ht, w, 3)
                if swap:
                    arr = arr[:, :, ::-1]
                buf[:ht, 58:] = arr.reshape(ht, stride)
                n = counter[0] % 16
                buf[ht, :field_len[n]] = field_arr[n]
                hd = hdrs[f][ht]
                hd.caplen = field_len[n]
                hd.len = field_len[n]
                for i in range(ht):
                    if lib.pcap_sendqueue_queue(q, brefs[f][i], cptrs[i]) != 0:
                        lib.pcap_sendqueue_destroy(q)
                        print("入队失败")
                        stop_ev.set()
                        return
                lib.pcap_sendqueue_queue(q, brefs[f][ht], cptrs[ht])
                counter[0] += 1
            while not stop_ev.is_set():
                try:
                    ck_q.put((q, len(frames)), timeout=0.5)
                    break
                except queue.Full:
                    continue

    threading.Thread(target=decode_loop, args=(spawn_video(),), daemon=True).start()
    threading.Thread(target=build_loop, daemon=True).start()

    print(f"{w}x{ht} source={a.video} fps={a.fps} gap={gap_us}us 分块 {chunk} 帧")
    print("接收卡必须持续收到帧才保持显示，正在持续发送…… Ctrl+C 停止")

    t_start = time.time()
    last = t_start
    sent = 0
    prev_t = None
    ivs = []
    try:
        while True:
            if os.path.exists(STOP_FILE):
                print("收到停止文件，正常退出")
                break
            try:
                q, n = ck_q.get(timeout=10)
            except queue.Empty:
                if stop_ev.is_set():
                    break
                continue
            rc = lib.pcap_sendqueue_transmit(h, q, 1)
            lib.pcap_sendqueue_destroy(q)
            now_t = time.perf_counter()
            if prev_t is not None:
                ivs.append(now_t - prev_t)
            prev_t = now_t
            if rc <= 0:
                msg = lib.pcap_geterr(h)
                print("发送失败:", msg.decode(errors="ignore") if msg else "")
                break
            sent += n
            now = time.time()
            if now - last >= 1.0:
                if ivs:
                    s = sorted(ivs)
                    med = s[len(s) // 2]
                    over = sum(1 for v in ivs if v > med * 1.5)
                    print(f"  实际 {sent / (now - t_start):.1f} fps  ({sent} 帧)  "
                          f"帧间隔 中位 {med * 1000:.2f}ms 最长 {s[-1] * 1000:.2f}ms "
                          f"超过1.5倍 {over}/{len(s)}", flush=True)
                    ivs.clear()
                else:
                    print(f"  实际 {sent / (now - t_start):.1f} fps  ({sent} 帧)", flush=True)
                last = now
            if a.seconds > 0 and now - t_start >= a.seconds:
                break
    except KeyboardInterrupt:
        print("\n中断")
    finally:
        stop_ev.set()
        while True:
            try:
                q, _ = ck_q.get_nowait()
                lib.pcap_sendqueue_destroy(q)
            except queue.Empty:
                break
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--iface", required=True)
    ap.add_argument("--width", type=int, default=480)
    ap.add_argument("--height", type=int, default=1080)
    ap.add_argument("--fps", type=float, default=30)
    ap.add_argument("--seconds", type=float, default=0, help="0=一直发到 Ctrl+C")
    ap.add_argument("--pattern", default="calib")
    ap.add_argument("--video", help="视频文件；自动居中裁满屏幕并循环播放")
    ap.add_argument("--ffmpeg", default=os.environ.get("FFMPEG", "ffmpeg"))
    ap.add_argument("--srcmac", default="6c:1f:f7:70:84:a4")
    ap.add_argument("--gap", type=int, default=0,
                    help="包间隔微秒；0=一次性突发（与 V960 一致），-1=均匀铺满帧周期")
    ap.add_argument("--swap", action="store_true",
                    help="交换 R/B 通道（实测本屏需要）")
    ap.add_argument("--chunk", type=int, default=30,
                    help="视频模式每块预构建帧数，默认 30")
    a = ap.parse_args()

    src = bytes(int(x, 16) for x in a.srcmac.split(":"))
    lib = ctypes.WinDLL(WPCAP, winmode=0x8)
    lib.pcap_open_live.restype = ctypes.c_void_p
    lib.pcap_open_live.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_char_p]
    lib.pcap_sendqueue_alloc.restype = ctypes.c_void_p
    lib.pcap_sendqueue_alloc.argtypes = [ctypes.c_uint]
    lib.pcap_sendqueue_queue.restype = ctypes.c_int
    lib.pcap_sendqueue_queue.argtypes = [ctypes.c_void_p, ctypes.POINTER(pkthdr), ctypes.c_char_p]
    lib.pcap_sendqueue_transmit.restype = ctypes.c_int
    lib.pcap_sendqueue_transmit.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
    lib.pcap_sendqueue_destroy.argtypes = [ctypes.c_void_p]
    lib.pcap_geterr.restype = ctypes.c_char_p
    lib.pcap_geterr.argtypes = [ctypes.c_void_p]
    lib.pcap_close.argtypes = [ctypes.c_void_p]

    err = ctypes.create_string_buffer(256)
    h = None
    # 上一个发送进程被强制结束后，Npcap 释放网卡句柄需要几秒，这里重试等待
    for attempt in range(20):
        h = lib.pcap_open_live(a.iface.encode(), 65536, 1, 10, err)
        if h:
            break
        time.sleep(2)
    if not h:
        print("打开网卡失败:", err.value.decode(errors="ignore"))
        print("（请等几秒后重试，或用管理员身份运行）")
        return 1

    w, ht = a.width, a.height
    stride = w * 3
    frame_bytes = stride * ht
    # gap=-1：把整帧所有包均匀铺满一个帧周期，由包时间戳自身决定节拍，
    # 绕过 Windows sleep 精度不足导致 60fps 抖动的问题
    if a.gap < 0:
        gap_us = max(1, int(round(1_000_000.0 / (a.fps * (ht + 1)))))
        auto_pace = True
    else:
        gap_us = a.gap
        auto_pace = False
    img = None if a.video else make_pattern(a.pattern, w, ht)
    if img is not None and a.swap:
        b = bytearray(len(img))
        b[0::3] = img[2::3]
        b[1::3] = img[1::3]
        b[2::3] = img[0::3]
        img = b

    def image_lines(data):
        return [eth(payload(LINE_TYPE, bytes(data[y * stride:(y + 1) * stride])), src)
                for y in range(ht)]

    lines = [] if img is None else image_lines(img)
    fields = [eth(payload(FIELD_TYPE, get_field_packet(n)), src) for n in range(16)]

    def build_queue(pkts):
        q = lib.pcap_sendqueue_alloc(sum(len(p) + 512 for p in pkts) + 4096)
        if not q:
            return None
        # 线速均匀节拍：第 i 个包在 i*gap 微秒后发出。
        # 一帧 1080 行 × 12µs ≈ 13ms，正好是千兆线速发完一帧的时间，
        # 与 V960 实际输出节奏一致（突发 13ms + 空闲 20ms）
        for i, p in enumerate(pkts):
            hd = pkthdr()
            hd.ts.s = 0
            hd.ts.u = i * gap_us
            hd.caplen = len(p)
            hd.len = len(p)
            if lib.pcap_sendqueue_queue(q, ctypes.byref(hd), p) != 0:
                return None
        return q

    def spawn_video():
        vf = (f"scale={w}:{ht}:force_original_aspect_ratio=increase,"
              f"crop={w}:{ht},fps={a.fps}")
        return subprocess.Popen([
            a.ffmpeg, "-loglevel", "error", "-i", a.video,
            "-vf", vf, "-an", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"
        ], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=frame_bytes)

    decoder = spawn_video() if a.video else None
    if decoder:
        return run_video(lib, h, a, w, ht, stride, frame_bytes, gap_us,
                         spawn_video, fields, src, a.swap)

    queues = [build_queue(lines + [fields[n]]) for n in range(16)]
    if any(q is None for q in queues):
        print("队列分配/入队失败")
        return 2

    source = a.video if a.video else "pattern=" + a.pattern
    print(f"{w}x{ht} source={source} fps={a.fps} gap={gap_us}us "
          f"每帧 {ht} 行包 + 1 场包，载荷 {frame_bytes/1e6:.2f} MB/帧")
    print("接收卡必须持续收到帧才保持显示，正在持续发送…… Ctrl+C 停止")

    total = int(a.seconds * a.fps) if a.seconds > 0 else -1
    t0 = time.time()
    sent = 0
    last = t0
    try:
        while total < 0 or sent < total:
            if os.path.exists(STOP_FILE):
                print("收到停止文件，正常退出")
                break
            q = queues[sent % 16]

            t1 = time.perf_counter()
            rc = lib.pcap_sendqueue_transmit(h, q, 0 if gap_us <= 0 else 1)
            dt = (time.perf_counter() - t1) * 1000
            if sent < 2:
                print(f"  单帧发送耗时 {dt:.1f} ms")
            if rc <= 0:
                msg = lib.pcap_geterr(h)
                print("发送失败:", msg.decode(errors="ignore") if msg else "")
                return 4
            sent += 1
            now = time.time()
            if now - last >= 1.0:
                print(f"  实际 {sent / (now - t0):.1f} fps  ({sent} 帧)", end="\r")
                last = now
            # 严格按设定帧率发送：落后时重置基准，绝不用追帧的方式补发，
            # 否则拔插网线等卡顿后会出现超速突发，画面必然闪烁
            # auto_pace 模式下整帧节拍已由包时间戳铺满，不能再叠加 sleep
            now = time.time()
            nxt = t0 + sent / a.fps
            if now - nxt > 0.5:
                t0 = now - sent / a.fps
            elif nxt > now and not auto_pace:
                time.sleep(nxt - now)
    except KeyboardInterrupt:
        print("\n中断")
    finally:
        for q in queues:
            if q:
                lib.pcap_sendqueue_destroy(q)
        lib.pcap_close(h)

    print(f"\n完成：{sent} 帧 / {time.time()-t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
