#!/bin/bash
# RK3588：X11 桌面指定区域 -> RGB24 -> 诺瓦接收卡单路直驱
set -euo pipefail

BASE=/userdata/nuowa
IFACE=${NOVA_IFACE:-eth0}
export DISPLAY=${NOVA_DISPLAY:-:0}
DESKTOP_W=${NOVA_DESKTOP_W:-1920}
DESKTOP_H=${NOVA_DESKTOP_H:-1080}
CROP_X=${NOVA_CROP_X:-0}
CROP_Y=${NOVA_CROP_Y:-0}
FPS=${NOVA_FPS:-60}
CAPTURE_FPS=${NOVA_CAPTURE_FPS:-65}

# 即使未接 HDMI，也把 X11 根画布扩到目标大小，避免默认 320x200 无法采集
xrandr --fb "${DESKTOP_W}x${DESKTOP_H}"

# 沿用 RK3588 卡莱特直驱的网卡与 CPU 稳定性设置
ip link set "$IFACE" up
ip link set "$IFACE" txqueuelen 10000
ethtool --set-eee "$IFACE" eee off 2>/dev/null || true
ethtool -A "$IFACE" autoneg off rx off tx off 2>/dev/null || true
ethtool -K "$IFACE" gro off gso off tso off 2>/dev/null || true
for governor in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
    echo performance > "$governor" 2>/dev/null || true
done

echo "桌面 $DISPLAY，区域 ${CROP_X},${CROP_Y} 起 480x1080，网口 $IFACE，${FPS}fps"
echo "当前诺瓦屏体使用 RGB 像素顺序；停止发送后接收卡会立即黑屏"

# x11grab 直接采集目标区域；采集略快于发送，用管道背压消除 X11 定时抖动造成的等帧
nice -n -5 taskset -c 4-5 ffmpeg -hide_banner -loglevel warning \
    -f x11grab -draw_mouse 1 -framerate "$CAPTURE_FPS" \
    -video_size 480x1080 -i "${DISPLAY}+${CROP_X},${CROP_Y}" \
    -an -pix_fmt rgb24 -f rawvideo - | \
    nice -n -10 taskset -c 6-7 "$BASE/nova_desktop" "$IFACE" 480 1080 "$FPS"
