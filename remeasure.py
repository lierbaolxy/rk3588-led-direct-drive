# -*- coding: utf-8 -*-
"""重新测量所有存图的各列亮度，校准列位置"""
import cv2, glob, os

# 细粒度竖条扫描：每40px一条，测整条中部亮度
for f in sorted(glob.glob(r"C:\Temp\*.jpg")):
    img = cv2.imread(f)
    if img is None:
        continue
    h, w = img.shape[:2]
    bars = []
    for x in range(0, w - 40, 40):
        v = img[int(h*0.45):int(h*0.75), x:x+40].mean()
        bars.append("%3d:%3.0f" % (x, v))
    print(os.path.basename(f), " | ".join(bars))
