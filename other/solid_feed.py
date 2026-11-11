# -*- coding: utf-8 -*-
"""纯色供帧实验：跳过 ffmpeg/nova_video，直接给 nova_desktop 喂纯色帧。
内核仍按全局时隙网格 30fps 发送。若纯色下仍闪黑 => 桌面/内核/链路/卡问题。"""
import os
import subprocess
import sys
import time

WIDTH, HEIGHT = 480, 1080
FRAME_BYTES = WIDTH * HEIGHT * 3
IFACE = os.environ.get("NOVA_IFACE", "eth0")
SENDER = os.environ.get("NOVA_SENDER", "/userdata/nuowa/kmod/nova_desktop")

# 纯绿帧
frame = bytes([0, 200, 0]) * (WIDTH * HEIGHT)

sender = subprocess.Popen([SENDER, IFACE, str(WIDTH), str(HEIGHT), "30", "30", "0"],
                          stdin=subprocess.PIPE,
                          stderr=open("/tmp/solid_sender.log", "wb"),
                          bufsize=FRAME_BYTES)
t0 = time.time()
n = 0
while time.time() - t0 < 75:
    try:
        sender.stdin.write(frame)
        sender.stdin.flush()
    except BrokenPipeError:
        break
    n += 1
    time.sleep(0.015)  # 供给快于消费，内核队列满时自然丢新帧
print("fed", n, file=sys.stderr)
sender.terminate()
