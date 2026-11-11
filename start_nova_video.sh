#!/bin/bash
# RK3588：480x1080 视频切片 -> RGB24 -> 诺瓦接收卡单路直驱，四机按固定墙钟对齐
set -euo pipefail

FILE=$1
IFACE=${NOVA_IFACE:-eth0}
export NOVA_SYNC_EPOCH=${NOVA_SYNC_EPOCH:-1767196800}
export NOVA_VIDEO_PERIOD_MS=${NOVA_VIDEO_PERIOD_MS:-16.667}
# 60fps 由 nova_desktop 内部时间戳控制，行包间隔 14µs 均匀铺开，不能改成突发
export NOVA_SENDER_FPS=${NOVA_SENDER_FPS:-60}
export NOVA_SENDER_GAP_US=${NOVA_SENDER_GAP_US:-14}

ip link set "$IFACE" up
ip link set "$IFACE" txqueuelen 10000
ethtool --set-eee "$IFACE" eee off 2>/dev/null || true
ethtool -A "$IFACE" autoneg off rx off tx off 2>/dev/null || true
ethtool -K "$IFACE" gro off gso off tso off 2>/dev/null || true
for governor in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
    echo performance > "$governor" 2>/dev/null || true
done

echo "视频 $FILE，网口 $IFACE，墙钟基准 $NOVA_SYNC_EPOCH，周期 ${NOVA_VIDEO_PERIOD_MS}ms"
exec nice -n -10 taskset -c 4-7 python3 /userdata/nuowa/nova_video.py "$FILE" 0
