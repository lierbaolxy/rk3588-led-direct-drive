#!/bin/bash
# 与卡莱特一致：客户端等待与主时钟收敛后，再按固定墙钟相位起播
set -euo pipefail

FILE=$1
export NOVA_SYNC_EPOCH=${NOVA_SYNC_EPOCH:-1767196800}

wait_clock() {
  # 27 是主时钟；它自身无需等待客户端时间源
  if ! chronyc sources 2>/dev/null | grep -q '172.20.32.27'; then
    return 0
  fi
  for i in $(seq 1 120); do
    tracking=$(chronyc tracking 2>/dev/null || true)
    ref=$(echo "$tracking" | awk '/^Reference ID/{print $4}')
    off=$(echo "$tracking" | awk '/^System time/{print $4}')
    upd=$(echo "$tracking" | awk '/^Update interval/{print $3}')
    if [ "$ref" = "AC14201B" ] && [ -n "$off" ] && [ -n "$upd" ] \
       && awk -v u="$upd" 'BEGIN{exit !(u > 0)}' \
       && awk -v o="$off" 'BEGIN{exit !((o < 0 ? -o : o) < 0.005)}'; then
      echo "时钟已收敛: ref=$ref off=${off}s upd=${upd}s"
      return 0
    fi
    sleep 1
  done
  echo "警告: 120 秒内时钟未收敛到 5ms，按当前时钟起播" >&2
}

wait_clock
exec /bin/bash /userdata/nuowa/start_nova_video.sh "$FILE"
