# -*- coding: utf-8 -*-
"""
实时镜像笔记本屏幕到接收卡（480x1080，严格 30Hz）
用法: python mirror_screen.py [输出序号] [帧周期ms] [stretch|letterbox|slice] [秒数,0=一直跑] [slice序号] [亮度]
架构:
  Capture 线程: 抓屏 -> 裁剪/缩放 -> BGR -> 原始帧队列
  Builder 线程: 取 30 帧 -> 建一个连续发送队列（帧间隔精确 period_us）
  Main 线程: 发送队列
"""
import sys
import os
import time
import pickle
import ctypes
import threading
import queue
from ctypes import c_int, c_uint, c_char_p, c_void_p, c_long, c_ubyte, POINTER, Structure
import numpy as np
import cv2
import dxcam

IFACE = os.environ.get(
    "CL_IFACE",
    r"\Device\NPF_{6AA96551-CF4C-4E4A-B138-55D0ADB4845B}",
)
WPCAP = r"C:\Windows\System32\Npcap\wpcap.dll"
WIDTH = 480
HEIGHT = 1080
ROW_LEN = 1461
CHUNK_FRAMES = 1
DST = bytes.fromhex("112233445566")
SRC = bytes.fromhex("222233445566")


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


def make_row_buffers():
    bufs = []
    for y in range(HEIGHT):
        buf = bytearray(ROW_LEN)
        buf[0:14] = DST + SRC + bytes([0x55, (y >> 8) & 0xFF])
        buf[14] = y & 0xFF
        buf[15] = 0
        buf[16] = 0
        buf[17] = (WIDTH >> 8) & 0xFF
        buf[18] = WIDTH & 0xFF
        buf[19] = 0x08
        buf[20] = 0x88
        bufs.append(buf)
    return bufs


class Capture(threading.Thread):
    def __init__(self, output_idx, fit, slice_index, out, stop):
        super().__init__(daemon=True)
        self.output_idx = output_idx
        self.fit = fit
        self.slice_index = slice_index
        self.out = out
        self.stop = stop
        self.fps = 0.0

    def _put_latest(self, raw):
        """队列满时丢掉旧帧，保证低延迟"""
        try:
            self.out.put_nowait(raw)
            return
        except queue.Full:
            pass
        try:
            self.out.get_nowait()
        except queue.Empty:
            pass
        try:
            self.out.put_nowait(raw)
        except queue.Full:
            pass

    def run(self):
        cam = dxcam.create(output_idx=self.output_idx)
        if cam is None:
            print("dxcam 创建失败")
            self.stop.set()
            return
        t_last = time.time()
        n = 0
        first = True
        last_raw = None
        while not self.stop.is_set():
            frame = cam.grab()
            if frame is None:
                # 画面静止时 DXGI 不产生新帧，复用上一帧保证连续输出
                if last_raw is not None:
                    self._put_latest(last_raw)
                else:
                    time.sleep(0.001)
                continue
            if frame.ndim == 3 and frame.shape[2] == 4:
                frame = frame[:, :, :3]

            if self.fit == "slice":
                h, w = frame.shape[:2]
                sw = max(1, w // 4)
                x0 = (self.slice_index - 1) * sw
                canvas = frame[:, x0:x0 + sw, :]
                if canvas.shape[0] != HEIGHT or canvas.shape[1] != WIDTH:
                    canvas = cv2.resize(canvas, (WIDTH, HEIGHT), interpolation=cv2.INTER_LINEAR)
            elif self.fit == "letterbox":
                h, w = frame.shape[:2]
                scale = min(WIDTH / w, HEIGHT / h)
                nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
                small = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_LINEAR)
                canvas = np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8)
                x0, y0 = (WIDTH - nw) // 2, (HEIGHT - nh) // 2
                canvas[y0:y0 + nh, x0:x0 + nw] = small
            else:
                canvas = cv2.resize(frame, (WIDTH, HEIGHT), interpolation=cv2.INTER_LINEAR)

            # dxcam 实测输出 RGB，接收卡按 BGR 解释
            canvas = canvas[:, :, ::-1]
            raw = canvas.tobytes()
            if len(raw) != WIDTH * HEIGHT * 3:
                continue
            if first:
                print("采集首帧", len(raw))
                first = False
            last_raw = raw

            self._put_latest(raw)

            n += 1
            now = time.time()
            if now - t_last >= 1.0:
                self.fps = n / (now - t_last)
                n = 0
                t_last = now


class Builder(threading.Thread):
    def __init__(self, lib, sync_tpl, rel_us, period_us, frames, ready, stop):
        super().__init__(daemon=True)
        self.lib = lib
        self.sync_tpl = sync_tpl
        self.rel_us = rel_us
        self.period_us = period_us
        self.frames = frames
        self.ready = ready
        self.stop = stop
        self.bufs = make_row_buffers()
        self.counter = 0
        self.fps = 0.0

    def run(self):
        t_last = time.time()
        built = 0
        while not self.stop.is_set():
            chunk = []
            while len(chunk) < CHUNK_FRAMES and not self.stop.is_set():
                try:
                    chunk.append(self.frames.get(timeout=0.5))
                except queue.Empty:
                    if self.stop.is_set():
                        break
            if not chunk:
                break
            print("收集 %d 帧，开始建队列" % len(chunk))

            n = len(chunk)
            mem = (16 + ROW_LEN) * HEIGHT * n + (16 + len(self.sync_tpl)) * n + 1024
            q = self.lib.pcap_sendqueue_alloc(mem)
            if not q:
                print("队列分配失败")
                self.stop.set()
                return

            for f, raw in enumerate(chunk):
                base = f * self.period_us
                for y in range(HEIGHT):
                    self.bufs[y][21:] = raw[y * WIDTH * 3:(y + 1) * WIDTH * 3]
                for y in range(HEIGHT):
                    hdr = PcapPkthdr(Timeval(0, base + self.rel_us[y]), ROW_LEN, ROW_LEN)
                    if self.lib.pcap_sendqueue_queue(
                            q, ctypes.byref(hdr),
                            (c_ubyte * ROW_LEN).from_buffer_copy(self.bufs[y])) != 0:
                        print("行包入队失败")
                        self.lib.pcap_sendqueue_destroy(q)
                        self.stop.set()
                        return
                sync = bytearray(self.sync_tpl)
                sync[14] = self.counter & 0xFF
                self.counter = (self.counter + 1) & 0xFF
                hdr = PcapPkthdr(Timeval(0, base + self.period_us), len(sync), len(sync))
                if self.lib.pcap_sendqueue_queue(
                        q, ctypes.byref(hdr),
                        (c_ubyte * len(sync)).from_buffer_copy(sync)) != 0:
                    print("同步包入队失败")
                    self.lib.pcap_sendqueue_destroy(q)
                    self.stop.set()
                    return

            while not self.stop.is_set():
                try:
                    self.ready.put((q, n), timeout=0.5)
                    break
                except queue.Full:
                    continue
            print("构建完成 %d 帧, %.1f MB" % (n, mem / 1024 / 1024))

            built += n
            now = time.time()
            if now - t_last >= 1.0:
                self.fps = built / (now - t_last)
                built = 0
                t_last = now


def main(output_idx=0, period_ms=33.333, fit="stretch", seconds=0.0, slice_index=4, brightness=None):
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "s6f_frame4.pkl"), "rb") as f:
        cap = pickle.load(f)
    sync_tpl = bytearray(cap["packets"][-1][1])
    if brightness is not None:
        sync_tpl[35] = brightness
        sync_tpl[38] = sync_tpl[39] = sync_tpl[40] = brightness
        print("亮度设为", brightness)
    rel_us = [int(round((t - cap["packets"][0][0]) * 1_000_000)) for t, _ in cap["packets"]]
    rel_us = rel_us[:HEIGHT]
    period_us = int(period_ms * 1000)

    lib = load_wpcap()
    errbuf = ctypes.create_string_buffer(256)
    pcap = lib.pcap_open_live(IFACE.encode("ascii"), 65536, 0, 1000, errbuf)
    if not pcap:
        print("打开网卡失败:", errbuf.value.decode(errors="ignore"))
        return 1

    frames = queue.Queue(maxsize=2)
    ready = queue.Queue(maxsize=2)
    stop = threading.Event()
    cap_thread = Capture(output_idx, fit, slice_index, frames, stop)
    cap_thread.start()
    builder = Builder(lib, sync_tpl, rel_us, period_us, frames, ready, stop)
    builder.start()

    sent = 0
    t_start = time.time()
    t_last = t_start
    print("镜像中: 输出 %d, 周期 %.3fms, 模式 %s, 每块 %d 帧" % (
        output_idx, period_ms, fit, CHUNK_FRAMES))

    try:
        while True:
            if seconds > 0 and time.time() - t_start >= seconds:
                break
            try:
                q, n = ready.get(timeout=15)
            except queue.Empty:
                print("等待队列超时")
                break
            sent_bytes = lib.pcap_sendqueue_transmit(pcap, q, 1)
            expected = n * 1596212
            if sent_bytes != expected:
                print("发送不完整: %d / %d 字节, 缺 %d" % (
                    sent_bytes, expected, expected - sent_bytes))
            lib.pcap_sendqueue_destroy(q)
            sent += n

            now = time.time()
            if sent % 30 < n:
                print("已发送 %d 帧, 发送 %.1f fps, 抓屏 %.1f fps, 构建 %.1f fps" % (
                    sent, sent / (now - t_start), cap_thread.fps, builder.fps))
    except KeyboardInterrupt:
        print("收到中断")
    finally:
        stop.set()
        lib.pcap_close(pcap)
        while True:
            try:
                q, _ = ready.get_nowait()
                lib.pcap_sendqueue_destroy(q)
            except queue.Empty:
                break

    print("结束: 共 %d 帧, 平均 %.1f fps" % (sent, sent / max(0.001, time.time() - t_start)))
    return 0


if __name__ == "__main__":
    out = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    per = float(sys.argv[2]) if len(sys.argv) > 2 else 33.333
    mode = sys.argv[3] if len(sys.argv) > 3 else "stretch"
    sec = float(sys.argv[4]) if len(sys.argv) > 4 else 0.0
    sli = int(sys.argv[5]) if len(sys.argv) > 5 else 4
    bri = int(sys.argv[6]) if len(sys.argv) > 6 and int(sys.argv[6]) > 0 else None
    sys.exit(main(out, per, mode, sec, sli, bri))
