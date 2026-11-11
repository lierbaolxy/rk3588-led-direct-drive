# -*- coding: utf-8 -*-
"""
卡莱特接收卡探测脚本（5A-75B 协议）
用途：笔记本网口直连接收卡后，发送发现包并解析接收卡回复。
协议依据：FPP ColorLight-5a-75.cpp 与 hkubota 的公开逆向资料。
"""
import sys
import time
import binascii
from scapy.all import Ether, Raw, sendp, sniff, conf

# 笔记本 Intel I219-V 对应的 Npcap 接口
IFACE = r"\Device\NPF_{6AA96551-CF4C-4E4A-B138-55D0ADB4845B}"

DST_MAC = "11:22:33:44:55:66"
SRC_MAC = "22:22:33:44:55:66"

DISC_TYPE = 0x07
DISC_SIZE = 284          # 整帧长度
RESP_TYPE = 0x08
RESP_SIZE = 1070


def build_discover(index=0):
    """构造发现包：EtherType=0x0700，data[3]=接收卡序号"""
    payload = bytearray(DISC_SIZE - 14)   # 270 字节
    payload[2] = index                    # data[3] 位于帧偏移 16
    return Ether(dst=DST_MAC, src=SRC_MAC, type=DISC_TYPE << 8) / Raw(bytes(payload))


def parse_response(raw):
    """按 FPP 的偏移解析接收卡回复"""
    if len(raw) < 100:
        return None
    data = raw[13:]  # data[0] 在帧偏移 13（EtherType 低字节）
    info = {
        "frame_len": len(raw),
        "eth_type": "0x%04x" % (raw[12] << 8 | raw[13]),
        "src": ":".join("%02x" % b for b in raw[6:12]),
        "dst": ":".join("%02x" % b for b in raw[0:6]),
        "card_type": data[0],
        "byte1": data[1],
        "fw_major": data[2],
        "fw_minor": data[3],
        "width": (data[21] << 8) | data[22],
        "height": (data[23] << 8) | data[24],
        "packets": (data[38] << 24) | (data[39] << 16) | (data[40] << 8) | data[41],
        "uptime_ms": (data[46] << 24) | (data[47] << 16) | (data[48] << 8) | data[49],
        "card_id": data[85] if len(data) > 85 else None,
    }
    return info


def main():
    conf.use_pcap = True
    print("接口:", IFACE)
    print("发送发现包 ...")

    # 连发 3 次，避免丢包
    for i in range(3):
        sendp(build_discover(0), iface=IFACE, verbose=False)
        time.sleep(0.2)

    print("等待接收卡回复（3 秒）...")
    pkts = sniff(iface=IFACE, timeout=3, store=True)

    found = 0
    for p in pkts:
        raw = bytes(p)
        if len(raw) < 14:
            continue
        eth_type = raw[12] << 8 | raw[13]
        if eth_type != RESP_TYPE << 8:   # 0x0805
            continue
        info = parse_response(raw)
        if not info:
            continue
        found += 1
        print("\n=== 接收卡回复 #%d ===" % found)
        for k, v in info.items():
            print("  %-10s %s" % (k + ":", v))
        print("  原始前 96 字节:", binascii.hexlify(raw[:96], " ").decode())
        with open("discover_response_%d.hex" % found, "wb") as f:
            f.write(binascii.hexlify(raw, " "))

    if not found:
        print("\n没有收到 0x0805 回复。可能原因：")
        print("  1) 网线没有接到接收卡输入端，或链路没起来")
        print("  2) 接收卡不是 5A-75B 系列，协议不同")
        print("  3) 接收卡没有供电")
        print("  4) Windows 网卡驱动没有允许原始帧发送")
        return 1

    print("\n共收到 %d 个回复。" % found)
    return 0


if __name__ == "__main__":
    sys.exit(main())
