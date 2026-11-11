# -*- coding: utf-8 -*-
"""
把任意图片按 S6F 实测协议发送给接收卡（480x1080，30Hz）
用法: python send_image.py <图片> [秒数]
同步包模板从 s6f_frame1.pkl 读取（抓包得到的原始同步包）
"""
import os
import sys
import time
import pickle
import ctypes
from ctypes import c_int, c_uint, c_char_p, c_void_p, c_long, c_ubyte, POINTER, Structure
from PIL import Image

IFACE = r"\Device\NPF_{6AA96551-CF4C-4E4A-B138-55D0ADB4845B}"
WPCAP = r"C:\Windows\System32\Npcap\wpcap.dll"
WIDTH = 480
HEIGHT = 1080
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


def build_row(row, pixels):
    """构造一个 1461 字节的行包"""
    frame = bytearray()
    frame += DST + SRC
    frame.append(0x55)
    frame.append((row >> 8) & 0xFF)
    frame.append(row & 0xFF)          # data[1] 行号低字节
    frame.append(0)                   # data[2] 像素偏移 MSB
    frame.append(0)                   # data[3] 像素偏移 LSB
    frame.append((WIDTH >> 8) & 0xFF) # data[4] 像素数 MSB
    frame.append(WIDTH & 0xFF)        # data[5] 像素数 LSB
    frame.append(0x08)
    frame.append(0x88)
    frame += pixels
    return bytes(frame)


def build_frame(img):
    rgb = img.convert("RGB").resize((WIDTH, HEIGHT))
    # 实测接收卡按 BGR 解释像素，交换红蓝通道
    r, g, b = rgb.split()
    rgb = Image.merge("RGB", (b, g, r))
    raw = rgb.tobytes()
    stride = WIDTH * 3
    packets = []
    for y in range(HEIGHT):
        packets.append((0, build_row(y, raw[y * stride:(y + 1) * stride])))
    return packets


def main(img_path, seconds=3.0):
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "s6f_frame1.pkl"), "rb") as f:
        cap = pickle.load(f)
    sync = bytearray(cap["packets"][-1][1])
    rel_us = [int(round((t - cap["packets"][0][0]) * 1_000_000)) for t, _ in cap["packets"]]

    img = Image.open(img_path)
    rows = build_frame(img)
    packets = rows + [(0, bytes(sync))]
    print("图片: %s, 行包 %d, 同步包 %d 字节" % (img_path, len(rows), len(sync)))

    period_us = 34000
    frames = max(1, int(seconds * 1_000_000 / period_us))
    mem = sum(len(raw) + 64 for _, raw in packets) * frames
    print("回放 %d 帧, 队列 %.1f MB" % (frames, mem / 1024 / 1024))

    lib = load_wpcap()
    errbuf = ctypes.create_string_buffer(256)
    pcap = lib.pcap_open_live(IFACE.encode("ascii"), 65536, 0, 1000, errbuf)
    if not pcap:
        print("打开网卡失败:", errbuf.value.decode(errors="ignore"))
        return 1
    q = lib.pcap_sendqueue_alloc(mem)
    if not q:
        print("分配队列失败")
        lib.pcap_close(pcap)
        return 1

    print("构建队列 ...")
    t0 = time.time()
    n = len(packets)
    for f in range(frames):
        base = f * period_us
        for i, (_, raw) in enumerate(packets):
            if i == n - 1:
                raw = bytearray(raw)
                raw[14] = f & 0xFF     # 帧计数器
                raw = bytes(raw)
            buf = (c_ubyte * len(raw)).from_buffer_copy(raw)
            hdr = PcapPkthdr(Timeval(0, base + rel_us[i]), len(raw), len(raw))
            if lib.pcap_sendqueue_queue(q, ctypes.byref(hdr), buf) != 0:
                print("入队失败 %d/%d" % (f, i))
                lib.pcap_sendqueue_destroy(q)
                lib.pcap_close(pcap)
                return 1
    print("队列完成 %.2fs, 开始发送 ..." % (time.time() - t0))

    sent = lib.pcap_sendqueue_transmit(pcap, q, 1)
    print("发送 %d 字节, 耗时 %.3fs" % (sent, time.time() - t0))

    lib.pcap_sendqueue_destroy(q)
    lib.pcap_close(pcap)
    return 0


if __name__ == "__main__":
    path = sys.argv[1]
    sec = float(sys.argv[2]) if len(sys.argv) > 2 else 3.0
    sys.exit(main(path, sec))
