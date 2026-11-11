# -*- coding: utf-8 -*-
"""
把抓包帧还原成图片，验证像素格式
用法: python render_frame.py <frame.pkl>
"""
import sys
import pickle
from PIL import Image


def main(pkl_path):
    with open(pkl_path, "rb") as f:
        data = pickle.load(f)

    rows = {}
    for _, raw in data["packets"]:
        et = (raw[12] << 8) | raw[13]
        if (et & 0xFF00) != 0x5500:
            continue
        row = ((et & 0xFF) << 8) | raw[14]
        pixels = raw[21:]
        rows[row] = pixels

    height = max(rows) + 1
    width = len(rows[0]) // 3
    print("还原尺寸: %d x %d, 行数 %d" % (width, height, len(rows)))

    for order, name in (("RGB", "frame_rgb.png"), ("BGR", "frame_bgr.png")):
        img = Image.new("RGB", (width, height))
        px = img.load()
        for y in range(height):
            p = rows.get(y)
            if not p:
                continue
            for x in range(width):
                b = p[x * 3:x * 3 + 3]
                if order == "BGR":
                    b = b[::-1]
                px[x, y] = tuple(b)
        img.save(name)
        print("已保存", name)

    # 采样几个像素，便于判断颜色顺序
    p0 = rows[0][:12]
    print("第 0 行前 4 像素原始字节:", p0.hex(" "))


if __name__ == "__main__":
    main(sys.argv[1])
