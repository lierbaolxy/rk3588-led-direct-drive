#!/bin/bash
# 用法: sudo bash start_video.sh <切片文件> [启动延迟秒] [启动时刻epoch]
IFACE=${CL_IFACE:-eth0}
FILE=$1
DELAY=${2:-5}
EPOCH=${3:-0}
echo $$ > /tmp/video.pid
ip link set "$IFACE" up
ip link set "$IFACE" txqueuelen 10000
ethtool --set-eee "$IFACE" eee off 2>/dev/null || true
ethtool -A "$IFACE" autoneg off rx off tx off 2>/dev/null || true
ethtool -K "$IFACE" gro off gso off tso off 2>/dev/null || true
for g in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do echo performance > "$g" 2>/dev/null || true; done
exec nice -n -10 taskset -c 4-7 env CL_IFACE="$IFACE" CL_FPS=60 CL_FRAME_OFFSET=1 CL_VIDEO_PERIOD_MS=16.667 CL_START_DELAY="$DELAY" CL_START_EPOCH="$EPOCH" \
    python3 /home/linaro/led/rk_send.py video "$FILE" 0
