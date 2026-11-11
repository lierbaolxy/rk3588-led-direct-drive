# -*- coding: utf-8 -*-
"""
把抓到的 S6F 帧原样回放给接收卡
用法: python replay_frame.py <frame.pkl> [秒数]
依赖: Npcap (wpcap.dll)，无需 scapy 发包
"""
import sys
import time
import pickle
import ctypes
from ctypes import c_int, c_uint, c_char_p, c_void_p, c_long, c_ubyte, POINTER, Structure

IFACE = r"\Device\NPF_{6AA96551-CF4C-4E4A-B138-55D0ADB4845B}"
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


def main(pkl_path, seconds=3.0, dry=False):
    with open(pkl_path, "rb") as f:
        data = pickle.load(f)
    packets = data["packets"]
    t0 = packets[0][0]
    rel_us = [int(round((t - t0) * 1_000_000)) for t, _ in packets]

    period_us = 34000          # 29.4Hz，略低于 30Hz，避免同步包与下一帧首包撞在同一时刻
    frames = max(1, int(seconds * 1_000_000 / period_us))
    total_pkts = len(packets) * frames
    mem = sum((len(raw) + 64) for _, raw in packets) * frames

    print("帧包数: %d, 回放帧数: %d, 总包数: %d, 队列 %.1f MB" % (
        len(packets), frames, total_pkts, mem / 1024 / 1024))

    lib = load_wpcap()
    errbuf = ctypes.create_string_buffer(256)
    pcap = lib.pcap_open_live(IFACE.encode("ascii"), 65536, 0, 1000, errbuf)
    if not pcap:
        print("打开网卡失败:", errbuf.value.decode(errors="ignore"))
        return 1

    q = lib.pcap_sendqueue_alloc(mem)
    if not q:
        print("分配发送队列失败")
        lib.pcap_close(pcap)
        return 1

    print("构建发送队列 ...")
    t_start = time.time()
    n_pkt = len(packets)
    for f in range(frames):
        base = f * period_us
        for i, (_, raw) in enumerate(packets):
            # 同步包（最后一个包）的 data[1] 是帧计数器，必须逐帧递增
            if i == n_pkt - 1:
                raw = bytearray(raw)
                raw[14] = f & 0xFF
                raw = bytes(raw)
            buf = (c_ubyte * len(raw)).from_buffer_copy(raw)
            hdr = PcapPkthdr(Timeval(0, base + rel_us[i]), len(raw), len(raw))
            if lib.pcap_sendqueue_queue(q, ctypes.byref(hdr), buf) != 0:
                print("入队失败: frame %d packet %d" % (f, i))
                lib.pcap_sendqueue_destroy(q)
                lib.pcap_close(pcap)
                return 1
    print("队列构建完成 %.2fs" % (time.time() - t_start))
    if dry:
        print("dry-run: 不发送，直接退出")
        lib.pcap_sendqueue_destroy(q)
        lib.pcap_close(pcap)
        return 0

    print("开始回放 ...")
    t_tx = time.time()
    sent = lib.pcap_sendqueue_transmit(pcap, q, 1)
    dt = time.time() - t_tx
    print("回放结束: 发送 %d 字节, 耗时 %.3fs" % (sent, dt))

    lib.pcap_sendqueue_destroy(q)
    lib.pcap_close(pcap)
    return 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    pkl = args[0]
    sec = float(args[1]) if len(args) > 1 else 3.0
    sys.exit(main(pkl, sec, "--dry" in sys.argv))
