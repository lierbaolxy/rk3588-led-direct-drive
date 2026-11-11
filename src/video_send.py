# -*- coding: utf-8 -*-
"""
卡莱特 S6F 协议视频直驱（480x1080 单路，约 29.4Hz）
  - 行包 1461 字节：目的 11:22:33:44:55:66，0x55 + 行号 + 偏移 + 像素数 + 0x08 0x88 + BGR
  - 同步包取抓包模板，第 14 字节逐帧递增作为帧计数器
  - 分块双缓冲：一块在发送时，下一块在后台构建，实测可稳定 29fps
用法: python video_send.py <视频或图片> [--loop]
"""
import argparse
import ctypes
import os
import pickle
import queue
import subprocess
import sys
import threading
import time
from ctypes import (POINTER, Structure, c_char_p, c_int, c_long, c_ubyte,
                    c_uint, c_void_p)

IFACE = os.environ.get("CL_IFACE",
                       r"\Device\NPF_{6AA96551-CF4C-4E4A-B138-55D0ADB4845B}")
WPCAP = r"C:\Windows\System32\Npcap\wpcap.dll"
WIDTH, HEIGHT = 480, 1080
STRIDE = WIDTH * 3
FRAME_BYTES = STRIDE * HEIGHT
DST = bytes.fromhex("112233445566")
SRC = bytes.fromhex("222233445566")


class Timeval(Structure):
    _fields_ = [("s", c_long), ("u", c_long)]


class PcapPkthdr(Structure):
    _fields_ = [("ts", Timeval), ("caplen", c_uint), ("len", c_uint)]


def load_wpcap():
    lib = ctypes.WinDLL(WPCAP)
    lib.pcap_open_live.argtypes = [c_char_p, c_int, c_int, c_int, c_char_p]
    lib.pcap_open_live.restype = c_void_p
    lib.pcap_sendqueue_alloc.argtypes = [c_uint]
    lib.pcap_sendqueue_alloc.restype = c_void_p
    lib.pcap_sendqueue_queue.argtypes = [c_void_p, POINTER(PcapPkthdr),
                                         POINTER(c_ubyte)]
    lib.pcap_sendqueue_queue.restype = c_int
    lib.pcap_sendqueue_transmit.argtypes = [c_void_p, c_void_p, c_int]
    lib.pcap_sendqueue_transmit.restype = c_uint
    lib.pcap_sendqueue_destroy.argtypes = [c_void_p]
    lib.pcap_close.argtypes = [c_void_p]
    return lib


ROW_PREFIX = [DST + SRC + bytes([0x55, y >> 8, y & 0xFF, 0, 0,
                                 WIDTH >> 8, WIDTH & 0xFF, 0x08, 0x88])
              for y in range(HEIGHT)]


class Decoder(threading.Thread):
    """ffmpeg 解码成 480x1080 BGR24 原始帧"""

    def __init__(self, path, fps, out, stop, loop):
        super().__init__(daemon=True)
        self.path = path
        self.fps = fps
        self.out = out
        self.stop = stop
        self.loop = loop
        self.is_image = os.path.splitext(path)[1].lower() in (
            ".png", ".jpg", ".jpeg", ".bmp")
        self.proc = None
        self.error = None
        self.eof = False

    def _spawn(self):
        vf = ("scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d,fps=%.4f"
              % (WIDTH, HEIGHT, WIDTH, HEIGHT, self.fps))
        cmd = [os.environ.get("FFMPEG", "ffmpeg"), "-loglevel", "error"]
        if self.is_image:
            cmd += ["-loop", "1"]
        cmd += ["-i", self.path, "-vf", vf, "-an",
                "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
        return subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, bufsize=FRAME_BYTES)

    def _read_frame(self):
        parts, got = [], 0
        while got < FRAME_BYTES:
            data = self.proc.stdout.read(FRAME_BYTES - got)
            if not data:
                return None
            parts.append(data)
            got += len(data)
        return b"".join(parts)

    def run(self):
        self.proc = self._spawn()
        while not self.stop.is_set():
            frame = self._read_frame()
            if frame is None:
                err = self.proc.stderr.read().decode(errors="ignore").strip()
                self.proc.wait()
                if not self.loop:
                    self.error = err or None
                    self.eof = True
                    break
                self.proc = self._spawn()
                continue
            while not self.stop.is_set():
                try:
                    self.out.put(frame, timeout=0.5)
                    break
                except queue.Full:
                    continue
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


class Builder(threading.Thread):
    def __init__(self, lib, frames_q, source_done, period_us, rel_us,
                 chunk_frames, out, stop):
        super().__init__(daemon=True)
        self.lib = lib
        self.frames_q = frames_q
        self.source_done = source_done
        self.period_us = period_us
        self.rel_us = rel_us
        self.chunk_frames = chunk_frames
        self.out = out
        self.stop = stop
        self.counter = 0
        self.error = False
        self.exhausted = False

    def _collect(self):
        rows = []
        while len(rows) < self.chunk_frames:
            try:
                rows.append(self.frames_q.get(timeout=0.5))
            except queue.Empty:
                if self.source_done.is_set() or len(rows) == 0 and self.exhausted:
                    break
        return rows

    def run(self):
        while not self.stop.is_set():
            frames = self._collect()
            if not frames:
                self.exhausted = True
                break
            mem = len(frames) * (HEIGHT + 1) * 1700
            q = self.lib.pcap_sendqueue_alloc(mem)
            if not q:
                self.error = True
                return
            for f, frame in enumerate(frames):
                base = f * self.period_us
                for y in range(HEIGHT):
                    raw = ROW_PREFIX[y] + frame[y * STRIDE:(y + 1) * STRIDE]
                    buf = (c_ubyte * len(raw)).from_buffer_copy(raw)
                    hdr = PcapPkthdr(Timeval(0, base + self.rel_us[y]),
                                     len(raw), len(raw))
                    if self.lib.pcap_sendqueue_queue(q, ctypes.byref(hdr), buf) != 0:
                        self.lib.pcap_sendqueue_destroy(q)
                        self.error = True
                        return
                self.counter = (self.counter + 1) & 0xFF
                sync = bytearray(SYNC_TEMPLATE)
                sync[14] = self.counter
                buf = (c_ubyte * len(sync)).from_buffer_copy(bytes(sync))
                hdr = PcapPkthdr(Timeval(0, base + self.rel_us[HEIGHT]),
                                 len(sync), len(sync))
                if self.lib.pcap_sendqueue_queue(q, ctypes.byref(hdr), buf) != 0:
                    self.lib.pcap_sendqueue_destroy(q)
                    self.error = True
                    return
            while not self.stop.is_set():
                try:
                    self.out.put((q, len(frames)), timeout=0.5)
                    break
                except queue.Full:
                    continue


SYNC_TEMPLATE = b""


def main():
    global SYNC_TEMPLATE
    ap = argparse.ArgumentParser()
    ap.add_argument("source")
    ap.add_argument("--loop", action="store_true", help="播完从头循环")
    ap.add_argument("--period-us", type=int, default=34000, help="帧周期，默认 34000")
    ap.add_argument("--chunk", type=int, default=88, help="每块帧数")
    a = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "s6f_frame1.pkl"), "rb") as f:
        cap = pickle.load(f)
    packets = cap["packets"]
    SYNC_TEMPLATE = bytes(packets[-1][1])
    rel_us = [int(round((t - packets[0][0]) * 1_000_000)) for t, _ in packets]

    fps = 1_000_000.0 / a.period_us
    lib = load_wpcap()
    errbuf = ctypes.create_string_buffer(256)
    pcap = lib.pcap_open_live(IFACE.encode("ascii"), 65536, 0, 1000, errbuf)
    if not pcap:
        print("打开网卡失败:", errbuf.value.decode(errors="ignore"))
        return 1

    stop = threading.Event()
    done = threading.Event()
    frames_q = queue.Queue(maxsize=12)
    out = queue.Queue(maxsize=2)
    dec = Decoder(a.source, fps, frames_q, stop, a.loop)
    bld = Builder(lib, frames_q, done, a.period_us, rel_us, a.chunk, out, stop)
    dec.start()
    bld.start()

    print("%dx%d %s %.2ffps 每帧 %d 行包 + 1 同步包" %
          (WIDTH, HEIGHT, a.source, fps, HEIGHT), flush=True)
    sent_frames = 0
    t_start = time.time()
    last_report = t_start
    try:
        while True:
            try:
                q, n = out.get(timeout=10)
            except queue.Empty:
                if not dec.is_alive():
                    done.set()
                if bld.exhausted or dec.error:
                    break
                if not bld.is_alive():
                    print("发送线程已退出")
                    break
                continue
            sent = lib.pcap_sendqueue_transmit(pcap, q, 1)
            lib.pcap_sendqueue_destroy(q)
            if sent == 0:
                print("发送失败（transmit 返回 0）")
                break
            sent_frames += n
            now = time.time()
            if now - last_report >= 2.0:
                print("累计 %d 帧, 平均 %.2f fps, 运行 %.1fs" %
                      (sent_frames, sent_frames / (now - t_start),
                       now - t_start), flush=True)
                last_report = now
            if bld.exhausted and out.empty():
                break
    except KeyboardInterrupt:
        print("收到中断")
    finally:
        stop.set()
        while True:
            try:
                q, _ = out.get_nowait()
                lib.pcap_sendqueue_destroy(q)
            except queue.Empty:
                break
        lib.pcap_close(pcap)
        if dec.error:
            print("解码失败:", dec.error)
    print("结束: 共发送 %d 帧, 运行 %.1fs" % (sent_frames, time.time() - t_start))
    return 0 if not bld.error else 1


if __name__ == "__main__":
    sys.exit(main())
