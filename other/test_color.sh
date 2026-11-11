#!/bin/bash
# 用法: test_color.sh R G B FPS
R=$1; G=$2; B=$3; FPS=${4:-60}
systemctl stop nova-wall.service 2>/dev/null || true
pkill -f 'geom.rgb' 2>/dev/null || true
pkill -f 'frame.rgb' 2>/dev/null || true
pkill -f 'color.rgb' 2>/dev/null || true
pkill -x nova_desktop 2>/dev/null || true
sleep 1
pkill -9 -f 'geom.rgb' 2>/dev/null || true
pkill -9 -f 'frame.rgb' 2>/dev/null || true
pkill -9 -f 'color.rgb' 2>/dev/null || true
pkill -9 -x nova_desktop 2>/dev/null || true
python3 -c "import sys;sys.stdout.buffer.write(bytes([$R,$G,$B])*(480*1080))" > /tmp/color.rgb
nohup sh -c "while true; do cat /tmp/color.rgb; done | /userdata/nuowa/nova_desktop eth0 480 1080 $FPS" > /tmp/color_test.log 2>&1 &
sleep 3
echo "pid=$(pgrep -x nova_desktop)"
tail -2 /tmp/color_test.log
