#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""诺瓦接收卡视频播放（480x1080 单列）：三线程流水线，解码/建包/发送并行。

  +----------+     +----------+     +----------+
  | ffmpeg   | --> | 建包线程 | --> | 发送线程 | --> eth0
  | 解码线程 |     | 1081 包  |     | transmit |
  +----------+     +----------+     +----------+

为什么三线程：单帧要建 1081 个包（约 10ms），发送又要占满一个帧周期（约 20ms），
串行只能跑到 30fps；建包与发送重叠后才能顶到 50fps 以上。

用法:
    python nova_video_live.py --video "874290021-1-208.mp4" --fps 50
停止: .stop_send 文件或 Ctrl+C
"""
import argparse
import ctypes
import os
import queue
import subprocess
import sys
import threading
import time

import numpy as np
import imageio_ffmpeg

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
        print(f"  打开网卡重试 {attempt + 1}/{retries}: "
              f"{err.value.decode('ascii', errors='replace')[:60]}", flush=True)
        time.sleep(1)
    return None


def precise_sleep(target):
    """睡到 target 时刻。Windows 的 sleep 精度约 15.6ms，
    直接 sleep(1ms) 可能变成睡 15.6ms，把帧节拍顶穿周期、造成闪帧。
    所以先粗睡到距目标 2ms 处，再忙等收尾。"""
    while True:
        now = time.perf_counter()
        rem = target - now
        if rem <= 0:
            return
        if rem > 0.002:
            time.sleep(rem - 0.002)


def read_exact(stream, size):
    parts = []
    got = 0
    while got < size:
        data = stream.read(size - got)
        if not data:
            return None
        parts.append(data)
        got += len(data)
    return b"".join(parts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--iface", default=IFACE)
    ap.add_argument("--srcmac", default="10:b6:76:42:42:8e")
    ap.add_argument("--width", type=int, default=480)
    ap.add_argument("--height", type=int, default=1080)
    ap.add_argument("--fps", type=float, default=50)
    ap.add_argument("--cropx", type=int, default=-1, help="-1=居中裁")
    ap.add_argument("--gap", type=int, default=0,
                    help="包间隔 us；0=自动(周期/包数)。调小可让整帧更快发完、留更多余量给抖动")
    ap.add_argument("--seconds", type=float, default=0)
    a = ap.parse_args()

    if not os.path.exists(a.video):
        print("找不到视频:", a.video, flush=True)
        return 1
    if os.path.exists(STOP_FILE):
        os.remove(STOP_FILE)

    w, ht = a.width, a.height
    stride = w * 3
    frame_bytes = stride * ht
    n_line = ht
    n_pkt = ht + 1
    period_us = int(round(1_000_000.0 / a.fps))
    # 向下取整：若向上取整，1081 包会铺超出帧周期，发送被拖慢
    # 下限 13us 是千兆线速（1498B 包 + 前导/帧间隙）的安全值
    if a.gap > 0:
        gap_us = a.gap
    else:
        gap_us = max(13, int(period_us // n_pkt))
    pkt_len = 58 + stride
    field_len = 58 + 1028
    cap = n_line * (pkt_len + 512) + field_len + 512 + 4096

    src = bytes(int(x, 16) for x in a.srcmac.split(":"))
    eth_head = DST_MAC + src + b"\xff\x00" + HEADER + bytes([LINE_TYPE])
    field_pkts = [DST_MAC + src + b"\xff\x00" + HEADER + bytes([FIELD_TYPE])
                  + make_field_data(n) for n in range(16)]

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    if a.cropx < 0:
        # 居中裁出 480 宽的竖条
        cropx_expr = "crop=%d:%d:(iw-%d)/2:0" % (w, ht, w)
    else:
        cropx_expr = "crop=%d:%d:%d:0" % (w, ht, a.cropx)
    vf = "%s,fps=%g,format=rgb24" % (cropx_expr, a.fps)

    def spawn():
        return subprocess.Popen(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-stream_loop", "-1",
             "-i", a.video, "-an", "-vf", vf,
             "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=frame_bytes * 2)

    print(f"[1/4] 加载 Npcap...", flush=True)
    lib = load_pcap()
    print(f"[2/4] 打开网卡...", flush=True)
    h = open_iface(lib, a.iface)
    if not h:
        print("打开网卡失败", flush=True)
        return 1

    print(f"[3/4] 分配 {4} 块缓冲并预置包头...", flush=True)
    bufs = []
    for _ in range(4):
        line = np.zeros((n_line, pkt_len), dtype=np.uint8)
        line[:, :58] = np.frombuffer(eth_head, dtype=np.uint8)
        fld = np.zeros(field_len, dtype=np.uint8)
        base = line.ctypes.data
        cptrs = [ctypes.cast(ctypes.cast(base + i * pkt_len,
                                        ctypes.POINTER(ctypes.c_ubyte)), ctypes.c_char_p)
                 for i in range(n_line)]
        fcptr = ctypes.cast(ctypes.cast(fld.ctypes.data, ctypes.POINTER(ctypes.c_ubyte)),
                            ctypes.c_char_p)
        hd = []
        for i in range(n_pkt):
            p = pkthdr()
            p.ts_s = 0
            p.ts_u = i * gap_us
            p.caplen = pkt_len if i < n_line else field_len
            p.len = p.caplen
            hd.append(p)
        bufs.append((line, fld, hd, cptrs, fcptr))

    print(f"[4/4] 启动流水线  fps={a.fps}  {w}x{ht}  "
          f"每帧 {n_pkt} 包 / {frame_bytes / 1e6:.2f} MB  理论 "
          f"{frame_bytes * 8 * a.fps / 1e6:.0f} Mbps  包间隔 {gap_us}us", flush=True)

    frame_q = queue.Queue(maxsize=4)
    send_q = queue.Queue(maxsize=2)
    stop = threading.Event()
    counter = [0]

    def decode_loop():
        proc = spawn()
        try:
            while not stop.is_set():
                data = read_exact(proc.stdout, frame_bytes)
                if data is None:
                    break
                while not stop.is_set():
                    try:
                        frame_q.put(data, timeout=0.3)
                        break
                    except queue.Full:
                        continue
        finally:
            try:
                proc.terminate()
            except OSError:
                pass

    def build_loop():
        n = 0
        while not stop.is_set():
            try:
                data = frame_q.get(timeout=0.3)
            except queue.Empty:
                continue
            k = n % 4
            line, fld, hd, cp, fcptr = bufs[k]
            line[:, 58:58 + stride] = np.frombuffer(data, dtype=np.uint8).reshape(n_line, stride)
            fno = counter[0] % 16
            fdata = field_pkts[fno]
            fld[:len(fdata)] = np.frombuffer(fdata, dtype=np.uint8)
            hd[n_line].caplen = len(fdata)
            hd[n_line].len = len(fdata)
            q = lib.pcap_sendqueue_alloc(cap)
            if not q:
                print("  队列分配失败", flush=True)
                stop.set()
                return
            ok = True
            for i in range(n_line):
                if lib.pcap_sendqueue_queue(q, ctypes.byref(hd[i]), cp[i]) != 0:
                    ok = False
                    break
            if ok:
                lib.pcap_sendqueue_queue(q, ctypes.byref(hd[n_line]), fcptr)
            if not ok:
                lib.pcap_sendqueue_destroy(q)
                print("  入队失败", flush=True)
                stop.set()
                return
            while not stop.is_set():
                try:
                    send_q.put(q, timeout=0.3)
                    break
                except queue.Full:
                    continue
            n += 1
            counter[0] += 1

    th_dec = threading.Thread(target=decode_loop, daemon=True)
    th_dec.start()
    th_b = threading.Thread(target=build_loop, daemon=True)
    th_b.start()

    sent = 0
    t_start = time.perf_counter()
    last_report = t_start
    last_frames = 0
    worst = 0.0
    try:
        while not stop.is_set():
            if os.path.exists(STOP_FILE):
                print("检测到 .stop_send，正常退出", flush=True)
                break
            try:
                q = send_q.get(timeout=3)
            except queue.Empty:
                if stop.is_set() or not th_b.is_alive():
                    print("建包线程停止", flush=True)
                    break
                continue
            t0 = time.perf_counter()
            rc = lib.pcap_sendqueue_transmit(h, q, 1)
            dt = time.perf_counter() - t0
            lib.pcap_sendqueue_destroy(q)
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
            # 帧节拍：gap 只决定一帧内 1081 个包铺开多快，周期由 fps 决定。
            # 铺开时间短、节拍长 → 每帧发完后有充足空闲，抖动不会顶穿周期。
            target = t_start + sent / a.fps
            if now - target > 0.5:
                t_start = now - sent / a.fps      # 严重落后就重置基准，不追帧
            elif target > now:
                precise_sleep(target)
            if now - last_report >= 1.0:
                print(f"  实测 {(sent - last_frames) / (now - last_report):.2f} fps  "
                      f"累计 {sent} 帧  本秒单帧 {dt * 1000:.2f}ms 最长 {worst * 1000:.2f}ms",
                      flush=True)
                last_report = now
                last_frames = sent
                worst = 0.0
    except KeyboardInterrupt:
        print("\n中断", flush=True)
    finally:
        stop.set()
        # 排空队列，避免泄漏
        while True:
            try:
                q = send_q.get_nowait()
                lib.pcap_sendqueue_destroy(q)
            except queue.Empty:
                break
        lib.pcap_close(h)

    total = time.perf_counter() - t_start
    print(f"结束：{sent} 帧 / {total:.1f}s = {sent / max(0.001, total):.2f} fps", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
