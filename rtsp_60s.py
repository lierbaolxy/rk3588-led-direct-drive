# -*- coding: utf-8 -*-
"""60秒长录：检测四列闪黑特征段及其发生时刻，暗帧存图"""
import cv2, time, sys, os

# 摄像头地址走环境变量，避免把账号密码写进仓库
URL = os.environ.get("LED_CAM_URL", "")
if not URL:
    sys.exit("请先设置 LED_CAM_URL，例如 rtsp://user:pass@192.168.1.64:554/Streaming/Channels/101")
DUR = 60.0
PATCHES = {
    "col1": (30, 340, 120, 460),
    "col2": (200, 320, 300, 440),
    "col3": (360, 300, 460, 420),
    "col4": (510, 280, 600, 400),
}

cap = cv2.VideoCapture(URL, cv2.CAP_FFMPEG)
if not cap.isOpened():
    print("RTSP 打开失败"); sys.exit(1)

t0 = time.time()
hist = {k: [] for k in PATCHES}
times = []
n = 0
saved = 0
while time.time() - t0 < DUR:
    ok, frame = cap.read()
    if not ok:
        continue
    n += 1
    ts = time.time() - t0
    times.append(ts)
    for k, (x1, y1, x2, y2) in PATCHES.items():
        hist[k].append(round(float(frame[y1:y2, x1:x2].mean()), 1))
    worst = min(hist[k][-1] for k in PATCHES)
    if worst < 25 and saved < 10 and ts > 10:  # 跳过开头10秒,抓中段证据
        _, buf = cv2.imencode(".jpg", frame)
        with open(r"C:\Temp\long_%05.1f.jpg" % ts, "wb") as f:
            f.write(buf.tobytes())
        saved += 1
cap.release()

print("总帧数", n)
for k, v in hist.items():
    segs = []
    i = 0
    while i < len(v) - 1:
        if v[i] < 25 and abs(v[i] - v[i+1]) < 3:
            j = i
            while j < len(v) - 1 and v[j+1] < 25:
                j += 1
            segs.append((round(times[i],1), round(times[j],1), round(min(v[i:j+1]),1)))
            i = j
        i += 1
    print("%s: min=%.1f 闪黑段=%d %s" % (k, min(v), len(segs), segs[:12]))
print("存图", saved)
