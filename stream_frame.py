# -*- coding: utf-8 -*-
"""
持续循环发送同一个抓包帧，让接收卡保持常亮
用法: python stream_frame.py <frame.pkl> [每块秒数] [总秒数，0=一直跑]
"""
import sys
import os
import time
import pickle
import ctypes
import threading
import queue
from ctypes import c_int, c_uint, c_char_p, c_void_p, c_long, c_ubyte, POINTER, Structure

IFACE = os.environ.get(
    "CL_IFACE",
    r"\Device\NPF_{6AA96551-CF4C-4E4A-B138-55D0ADB4845B}",
)
WPCAP = r"C:\Windows\System32\Npcap\wpcap.dll"


class Timeval(Structure):
    _fields_ = [("tv_sec", c_long), ("tv_usec", c_long)]


class PcapPkthdr(Structure):
    _fields_ = [("ts", Timeval), ("caplen", c_uint), ("len", c_uint)]


def load_wpcap():
    lib = ctypes.WinDLL(WPCAP)
    lib.pcap_open_live.argtypes = [c_char_p, c_int, c_int, c_int, c_char_p]
    lib.pcap_open_live.restype = c_void_p
    lib.pcap_sendqueue_alloc.argtypes = [c_uint]
    lib.pcap_sendqueue_alloc.restype = c_void_p
    lib.pcap_sendqueue_queue.argtypes = [c_void_p, POINTER(PcapPkthdr), POINTER(c_ubyte)]
    lib.pcap_sendqueue_queue.restype = c_int
    lib.pcap_sendqueue_transmit.argtypes = [c_void_p, c_void_p, c_int]
    lib.pcap_sendqueue_transmit.restype = c_uint
    lib.pcap_sendqueue_destroy.argtypes = [c_void_p]
    lib.pcap_close.argtypes = [c_void_p]
    return lib


def build_chunk(lib, packets, rel_us, period_us, frames, counter_start):
    mem = sum(len(raw) + 64 for _, raw in packets) * frames
    q = lib.pcap_sendqueue_alloc(mem)
    if not q:
        return None, 0, 0
    n = len(packets)
    for f in range(frames):
        base = f * period_us
        for i, (_, raw) in enumerate(packets):
            if i == n - 1:
                raw = bytearray(raw)
                raw[14] = (counter_start + f) & 0xFF
                raw = bytes(raw)
            buf = (c_ubyte * len(raw)).from_buffer_copy(raw)
            hdr = PcapPkthdr(Timeval(0, base + rel_us[i]), len(raw), len(raw))
            if lib.pcap_sendqueue_queue(q, ctypes.byref(hdr), buf) != 0:
                lib.pcap_sendqueue_destroy(q)
                return None, 0, 0
    return q, frames, mem


class Builder(threading.Thread):
    def __init__(self, lib, packets, rel_us, period_us, frames, out, stop):
        super().__init__(daemon=True)
        self.lib = lib
        self.packets = packets
        self.rel_us = rel_us
        self.period_us = period_us
        self.frames = frames
        self.out = out
        self.stop = stop
        self.counter = 0
        self.error = False

    def run(self):
        while not self.stop.is_set():
            q, n, mem = build_chunk(self.lib, self.packets, self.rel_us,
                                    self.period_us, self.frames, self.counter)
            if not q:
                self.error = True
                return
            self.counter = (self.counter + n) & 0xFF
            while not self.stop.is_set():
                try:
                    self.out.put((q, n, mem), timeout=0.5)
                    break
                except queue.Full:
                    continue


def main(pkl_path, chunk_sec=2.0, total_sec=0.0):
    with open(pkl_path, "rb") as f:
        data = pickle.load(f)
    packets = data["packets"]
    t0 = packets[0][0]
    rel_us = [int(round((t - t0) * 1_000_000)) for t, _ in packets]
    period_us = 34000
    frames_per_chunk = max(1, int(chunk_sec * 1_000_000 / period_us))

    lib = load_wpcap()
    errbuf = ctypes.create_string_buffer(256)
    pcap = lib.pcap_open_live(IFACE.encode("ascii"), 65536, 0, 1000, errbuf)
    if not pcap:
        print("打开网卡失败:", errbuf.value.decode(errors="ignore"))
        return 1

    out = queue.Queue(maxsize=2)
    stop = threading.Event()
    builder = Builder(lib, packets, rel_us, period_us, frames_per_chunk, out, stop)
    builder.start()

    print("持续发送: 每块 %d 帧 (%.1fs), 总时长 %s" % (
        frames_per_chunk, frames_per_chunk * period_us / 1e6,
        "不限" if total_sec <= 0 else "%.0fs" % total_sec))
    deadline = time.time() + total_sec if total_sec > 0 else None
    sent_frames = 0
    t_start = time.time()

    try:
        while deadline is None or time.time() < deadline:
            q, n, mem = out.get(timeout=10)
            t_tx = time.time()
            sent = lib.pcap_sendqueue_transmit(pcap, q, 1)
            dt = time.time() - t_tx
            lib.pcap_sendqueue_destroy(q)
            sent_frames += n
            if sent == 0:
                print("发送失败（transmit 返回 0）")
                break
            print("块完成: %d 帧, %.1f MB, 用时 %.2fs, 累计 %d 帧, 运行 %.1fs" % (
                n, mem / 1024 / 1024, dt, sent_frames, time.time() - t_start))
    except KeyboardInterrupt:
        print("收到中断")
    except queue.Empty:
        print("队列超时，发送线程异常")
    finally:
        stop.set()
        while True:
            try:
                q, _, _ = out.get_nowait()
                lib.pcap_sendqueue_destroy(q)
            except queue.Empty:
                break
        lib.pcap_close(pcap)

    print("结束: 共发送 %d 帧, 运行 %.1fs" % (sent_frames, time.time() - t_start))
    return 0 if not builder.error else 1


if __name__ == "__main__":
    args = sys.argv[1:]
    pkl = args[0]
    chunk = float(args[1]) if len(args) > 1 else 2.0
    total = float(args[2]) if len(args) > 2 else 0.0
    sys.exit(main(pkl, chunk, total))
