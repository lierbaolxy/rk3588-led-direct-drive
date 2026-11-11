#!/bin/bash
# 停止测试发送并关闭自启，避免与 V960 抢占接收卡
systemctl stop nova-wall.service 2>/dev/null || true
systemctl disable nova-wall.service >/dev/null 2>&1 || true
pkill -f 'geom.rgb' 2>/dev/null || true
pkill -f 'frame.rgb' 2>/dev/null || true
pkill -x nova_desktop 2>/dev/null || true
sleep 1
pkill -9 -f 'geom.rgb' 2>/dev/null || true
pkill -9 -f 'frame.rgb' 2>/dev/null || true
pkill -9 -x nova_desktop 2>/dev/null || true
sleep 1
echo "nova_desktop=$(pgrep -c -x nova_desktop || echo 0)"
echo "service=$(systemctl is-active nova-wall.service)"
echo "enabled=$(systemctl is-enabled nova-wall.service 2>/dev/null || echo disabled)"
