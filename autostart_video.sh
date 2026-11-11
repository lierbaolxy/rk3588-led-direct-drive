#!/bin/bash
# 开机自启：四台共用同一个固定基准 T0（2026-01-01 00:00:00 +08:00）
# 起播时按“墙钟时隙 mod 文件总帧数”跳到文件对应位置，
# 因此任何一台在任何时刻重启，内容都会自动落回与其余三台相同的相位。
FILE=$1
T0=1767196800

# 时钟闸门：必须等 chrony 真正和主时钟（177）对上、偏差 <5ms 才起播。
# 旧版只看 System time：未同步时它显示 0，闸门直接放行；结果 RTC 的 1.1 秒误差
# 会在起播约 20 秒后被 chrony 步进纠正，内容相位被永久推前约 68 帧（1.1 秒），
# 而 catchup 只能向前跳帧、修不了“内容超前”，只能靠重启或程序内 resync 纠偏。
wait_clock() {
  # 本机没有上游源（177 这类主时钟），不需要等
  if ! chronyc sources 2>/dev/null | grep -q '172.20.33.177'; then
    return 0
  fi
  for i in $(seq 1 120); do
    t=$(chronyc tracking 2>/dev/null)
    ref=$(echo "$t" | awk '/^Reference ID/{print $4}')
    off=$(echo "$t" | awk '/^System time/{print $4}')
    upd=$(echo "$t" | awk '/^Update interval/{print $3}')
    if [ "$ref" = "AC1421B1" ] && [ -n "$off" ] && [ -n "$upd" ] \
       && awk -v u="$upd" 'BEGIN{exit !(u > 0)}' \
       && awk -v o="$off" 'BEGIN{exit !(o < 0.005)}'; then
      echo "时钟已收敛: ref=$ref off=${off}s upd=${upd}s"
      return 0
    fi
    sleep 1
  done
  echo "警告: 120 秒内时钟未收敛到 5ms，按当前时钟起播" >&2
  return 0
}

wait_clock
exec /bin/bash /home/linaro/led/start_video.sh "$FILE" 0 "$T0"
