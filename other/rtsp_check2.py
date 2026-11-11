# -*- coding: utf-8 -*-
"""录 20 秒，凡 col2 亮度<45 就存全帧+打时间戳，人工确认是黑屏还是暗内容"""
import cv2
import os
import time
import sys

# 摄像头地址走环境变量，避免把账号密码写进仓库
URL = os.environ.get("LED_CAM_URL", "")
if not URL:
    sys.exit("请先设置 LED_CAM_URL，例如 rtsp://user:pass@192.168.1.64:554/Streaming/Channels/101")
DUR = 20.0
P = (200, 320, 300, 440)  # col2 patch

cap = cv2.VideoCapture(URL, cv2.CAP_FFMPEG)
if not cap.isOpened():
    print("RTSP 打开失败")
    sys.exit(1)

t0 = time.time()
n = 0
saved = 0
while time.time() - t0 < DUR:
    ok, frame = cap.read()
    if not ok:
        continue
    n += 1
    ts = time.time() - t0
    x1, y1, x2, y2 = P
    v = float(frame[y1:y2, x1:x2].mean())
    if v < 45 and saved < 12:
        _, buf = cv2.imencode(".jpg", frame)
        with open(r"C:\Temp\dark_%05.1f.jpg" % ts, "wb") as f:
            f.write(buf.tobytes())
        print("暗帧 t=%.2f 亮度=%.1f" % (ts, v))
        saved += 1
cap.release()
print("总帧数", n, "存图", saved)
