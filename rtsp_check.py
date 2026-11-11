# -*- coding: utf-8 -*-
"""录 20 秒摄像头 RTSP，逐帧测第2列(.28新模块)和第3列(.27旧模块)区域亮度，检测闪黑"""
import cv2
import os
import time
import sys

# 摄像头地址走环境变量，避免把账号密码写进仓库
URL = os.environ.get("LED_CAM_URL", "")
if not URL:
    sys.exit("请先设置 LED_CAM_URL，例如 rtsp://user:pass@192.168.1.64:554/Streaming/Channels/101")
DUR = 20.0

# 1024x576 画面里大屏倾斜，取各列中部小块
PATCHES = {
    "col2_new": (200, 320, 300, 440),   # 第2列 .28 新模块
    "col3_old": (360, 300, 460, 420),   # 第3列 .27 旧模块
    "col4_old": (510, 280, 610, 400),   # 第4列 .31 旧模块
}

cap = cv2.VideoCapture(URL, cv2.CAP_FFMPEG)
if not cap.isOpened():
    print("RTSP 打开失败")
    sys.exit(1)

t0 = time.time()
rows = []
saved = 0
while time.time() - t0 < DUR:
    ok, frame = cap.read()
    if not ok:
        continue
    ts = time.time() - t0
    vals = {}
    for name, (x1, y1, x2, y2) in PATCHES.items():
        patch = frame[y1:y2, x1:x2]
        vals[name] = float(patch.mean())
    rows.append((ts, vals))
    # 每 ~2 秒存一张全帧
    if ts - saved >= 2.0:
        cv2.imwrite(r"o:\发送卡\发送卡\rt_%05.1f.jpg" % ts, frame)
        saved = ts

cap.release()
print("共 %d 帧" % len(rows))
print("时间  " + "  ".join("%8s" % k for k in PATCHES))
black = {k: 0 for k in PATCHES}
for ts, v in rows:
    line = "%5.1f " % ts + "  ".join("%8.1f" % v[k] for k in PATCHES)
    for k in PATCHES:
        if v[k] < 12:
            black[k] += 1
    print(line)
print("亮度<12(近黑)帧数:", black)
