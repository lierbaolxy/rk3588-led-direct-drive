#!/bin/bash
# 按指定帧率重发定位测试图，用于 30fps / 60fps 对比
FPS=${1:-30}
systemctl stop nova-wall.service 2>/dev/null || true
pkill -f 'cat /tmp/geom.rgb' 2>/dev/null || true
pkill -x nova_desktop 2>/dev/null || true
sleep 1
pkill -9 -f 'cat /tmp/geom.rgb' 2>/dev/null || true
pkill -9 -x nova_desktop 2>/dev/null || true
nohup sh -c "while true; do cat /tmp/geom.rgb; done | /userdata/nuowa/nova_desktop eth0 480 1080 $FPS" > /tmp/geom_test.log 2>&1 &
sleep 4
pgrep -a -x nova_desktop
tail -2 /tmp/geom_test.log
