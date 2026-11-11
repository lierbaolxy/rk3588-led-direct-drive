# -*- coding: utf-8 -*-
"""修正版：1920x1080 实际分辨率，四列正确采样。录60秒+闪黑段时刻+稳定性判据"""
import cv2, time, sys, os

# 摄像头地址走环境变量，避免把账号密码写进仓库
URL = os.environ.get("LED_CAM_URL", "")
if not URL:
    sys.exit("请先设置 LED_CAM_URL，例如 rtsp://user:pass@192.168.1.64:554/Streaming/Channels/101")
DUR = 60.0
PATCHES = {
    "col1_30": (80, 500, 280, 800),     # col1 = .30
    "col2_28": (400, 500, 600, 800),    # col2 = .28
    "col3_27": (720, 500, 920, 800),    # col3 = .27
    "col4_31": (1030, 500, 1230, 800),  # col4 = .31
}

cap = cv2.VideoCapture(URL, cv2.CAP_FFMPEG)
if not cap.isOpened():
    print("RTSP 打开失败"); sys.exit(1)
w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)); h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
print("流分辨率 %dx%d" % (w, h))

t0 = time.time()
hist = {k: [] for k in PATCHES}
times = []
n = 0
while time.time() - t0 < DUR:
    ok, frame = cap.read()
    if not ok:
        continue
    n += 1
    ts = time.time() - t0
    times.append(ts)
    for k, (x1, y1, x2, y2) in PATCHES.items():
        hist[k].append(round(float(frame[y1:y2, x1:x2].mean()), 1))
cap.release()

print("总帧数", n, "时长 %.1fs" % (times[-1] if times else 0))
for k, v in hist.items():
    segs = []
    i = 0
    while i < len(v) - 1:
        if v[i] < 30 and abs(v[i] - v[i+1]) < 4:
            j = i
            while j < len(v) - 1 and v[j+1] < 30:
                j += 1
            segs.append((round(times[i],1), round(times[j],1), round(min(v[i:j+1]),1)))
            i = j
        i += 1
    print("%s: min=%.1f 闪黑段=%d %s" % (k, min(v), len(segs), segs[:15]))
