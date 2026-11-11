# RK3588 四机直驱 1920×1080 LED 大屏 · 部署与排障 SOP

> 适用场景：用 4 台 RK3588（Debian 11）盒子各走一个千兆网口，直连卡莱特接收卡链，替代 S6F 发送卡点亮 1920×1080 拼接大屏。
> 当前状态：四块 60fps 稳定运行，国产机顶盒不再需要发送卡。
> 约束：**不要发送 0x02 布局配置包**；接收卡参数保持 LEDSetting 原有配置不变。

---

## 1. 系统架构

```
源视频 3840×2160@29.97fps
        │  按列切成 4 段，每段 480×1080，再重编码为 60fps
        ▼
┌──────────┬──────────┬──────────┬──────────┐
│  盒子80  │  盒子85  │  盒子95  │ 盒子177  │
│ duck_p2  │ duck_p1  │ duck_p3  │ duck_p4  │
│  480×1080│  480×1080│  480×1080│  480×1080│
└────┬─────┴────┬─────┴────┬─────┴────┬─────┘
     │eth0      │eth0      │eth0      │eth0      各 1 根网线
     ▼          ▼          ▼          ▼
  接收卡链1   接收卡链2   接收卡链3   接收卡链4
     │          │          │          │
   第1块(最左) 第2块      第3块      第4块(最右)
```

| 设备 | IP | 登录名 | 播放切片 | 屏幕位置 | 切片存放路径 |
|---|---|---|---|---|---|
| 80 | 172.20.33.80 | linaro | duck_p2_60.mkv | 第 1 块（最左） | `/userdata/led_video/` |
| 85 | 172.20.33.85 | linaro | duck_p1_60.mkv | 第 2 块 | `/userdata/led_video/` |
| 95 | 172.20.33.95 | linaro | duck_p3_60.mkv | 第 3 块 | `/userdata/led_video/` |
| 177 | 172.20.33.177 | linaro | duck_p4_60.mkv | 第 4 块（最右） | `/userdata/zxbox/video/split/` |

> **重要**：第 1、2 块的切片编号是**反的**（80 播 p2、85 播 p1）。这是现场核对过的正确对应，不要按直觉“纠正”。

---

## 2. 关键参数总表（可直接对照）

| 参数 | 取值 | 说明 / 边界 |
|---|---|---|
| 单路分辨率 | 480×1080 | 整屏 1920×1080 = 4 路横向拼接 |
| 发送帧率 | 59.94 fps（周期 **16.683 ms**） | 30fps 时周期为 33.367ms |
| 每帧包数 | 1081 包 | 1080 行包 + 1 同步包 |
| 行包大小 | 1461 字节 | 14 以太头 + 7 协议头 + 1440 BGR 像素 |
| 每帧数据量 | 约 1.58 MB | 1081 × 1461 B ≈ 1.58 MB |
| 平均带宽（60fps） | 758 Mbps | 30fps 时约 379 Mbps |
| 突发带宽 | 约 970 Mbps，持续约 13 ms | 空闲仅 3.7 ms（60fps）；30fps 空闲 20.3 ms |
| 网卡队列 | `txqueuelen 10000` | 太小会直接丢包 |
| CPU 绑核 | `taskset -c 4-7`（A76 大核） | 绑小核会发不完一帧 |
| 进程优先级 | `nice -n -10` | 需要 root 才能设负值 |
| CPU 调频 | `performance`（满频 2.25–2.35GHz） | **断电会丢，必须写进启动脚本** |
| 网卡 EEE | 关闭（`eee off`） | 开启会频闪 |
| 网卡流控/offload | 全关（autoneg/rx/tx、gro/gso/tso） | 开启会抖动丢包 |
| 时钟同步 | chrony，偏差 <1 ms | 177 做本地 NTP 服务器；**起播前必须等“真正对上 177”的偏差 <5ms**，否则会踩 #18 |
| 起播对齐 | 固定常量 `T0=1767196800`；`文件帧 = (now-T0)/16.683ms mod 61086` | 起播时 `-ss` 跳到该帧，任意时刻重启都自动对齐，不需要四台一起 |
| 像素顺序 | BGR | 接收卡要求，RGB 会偏色 |
| 亮度 | 同步包显式下发 | 不随发送端配置自动同步 |

---

## 3. 部署流程

### 阶段 A：准备

1. 4 台盒子通电、接网线（每台 eth0 单独一根直连接收卡链），配置静态 IP 到同一网段。
2. 确认每台可 SSH 登录、`/userdata` 有足够空间（60fps 切片每个约 430–470MB）。
3. 确认源视频可读：`/userdata/zxbox/video/duck.mp4`（3840×2160@29.97fps，3 分 42 秒）。

校验：

```bash
ssh linaro@172.20.33.80 'df -h /userdata | tail -1; ls -l /userdata/zxbox/video/'
```

### 阶段 B：视频切片

1. 把源视频按列切成 4 段 480×1080（p1 最左 … p4 最右），29.97fps，封装 **MKV**（MP4 切片在本机播放会坏，不要用）。
2. 再把每段重编码为 **60fps**（源只有 29.97fps，用 `fps=60` 补帧到 60）：

```bash
# 60fps 重编码（硬件编码，单段约 20–30 秒）
ffmpeg -y -loglevel error -i <源切片>.mkv \
  -vf fps=60 -c:v h264_rkmpp -b:v 20M -an \
  <输出>_60.mkv
```

3. 校验切片参数（必须 480×1080、60/1）：

```bash
ffprobe -v error -select_streams v:0 \
  -show_entries stream=width,height,r_frame_rate,avg_frame_rate \
  -of default=nw=1 <文件>
```

4. 分发到各台，建议走 177 起临时 HTTP 服务 + `wget`（WiFi 直接 SFTP 大文件会断流）。传完必须 **md5 校验**。

### 阶段 C：时钟同步

177 作为本地 NTP 服务器，其余三台指向它（同一墙钟是一切对齐的前提）。

177（`/etc/chrony/chrony.conf` 末尾追加）：

```
allow 172.20.32.0/23
local stratum 10
```

80/85/95（`/etc/chrony/chrony.conf`）：

```
server 172.20.33.177 iburst prefer
```

```bash
sudo systemctl restart chrony
chronyc tracking | grep -E "System time|RMS"
```

合格：`System time` 偏差在微秒~亚毫秒级（实测 3–7 μs）。

### 阶段 D：发送脚本与自启服务

每台两个脚本 + 一个服务单元。**脚本必须 LF 换行**（Windows 换行会让服务报 127 起不来）。

`/home/linaro/led/start_video.sh`：

```bash
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
exec nice -n -10 taskset -c 4-7 env CL_IFACE="$IFACE" CL_VIDEO_PERIOD_MS=16.683 CL_START_DELAY="$DELAY" CL_START_EPOCH="$EPOCH" \
    python3 /home/linaro/led/rk_send.py video "$FILE" 0
```

`/home/linaro/led/autostart_video.sh`（开机自启入口，四台一致）：

```bash
#!/bin/bash
# 开机自启：四台共用同一个固定基准 T0（2026-01-01 00:00:00 +08:00）
# 起播时按“墙钟时隙 mod 文件总帧数”跳到文件对应位置，
# 因此任何一台在任何时刻重启，内容都会自动落回与其余三台相同的相位。
FILE=$1
T0=1767196800

# 时钟闸门：必须等 chrony 真正和主时钟（177）对上、偏差 <5ms 才起播。
# 旧版只看 System time：未同步时它显示 0，闸门直接放行；结果 RTC 的 1.1 秒误差
# 会在起播约 20 秒后被 chrony 步进纠正，内容相位被永久推前约 68 帧（1.1 秒），
# 而 catchup 只能向前跳帧、修不了“内容超前”（见问题 #18）。
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
```

> `Reference ID` 里的 `AC1421B1` 就是 172.20.33.177 的十六进制写法（AC=172, 14=20, 21=33, B1=177）。
> 未同步时 chrony 显示 `7F7F0101`、`Update interval 0.0`，`System time` 也是 0——这正是旧闸门失效的原因。
> 手动调试直接跑 `start_video.sh` 会绕过这道闸门，屏幕上出现莫名偏 1 秒时先怀疑这一点。

> 配套改了两处：`rk_send.py` 的 `VideoSource` 支持 `-ss <秒>` 输入 seek；起播时先 ffprobe 取片长与帧率，
> 算出 `时隙 = (now-T0)/16.683ms`、`文件帧 = 时隙 mod 总帧数`，再 seek 到 `文件帧/60` 秒。
> 四台的客户端 `chrony.conf` 改为 `server 172.20.33.177 iburst prefer minpoll 4 maxpoll 6`（16–64s 轮询，收敛更快）。

`/etc/systemd/system/led-wall.service`（**ExecStart 的切片路径每台不同**）：

```ini
[Unit]
Description=LED Wall Video
After=network-online.target chrony.service
Wants=network-online.target chrony.service

[Service]
Type=simple
ExecStart=/bin/bash /home/linaro/led/autostart_video.sh <本机60fps切片绝对路径>
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

对应关系：

| 设备 | ExecStart 路径 |
|---|---|
| 80 | `/userdata/led_video/duck_p2_60.mkv` |
| 85 | `/userdata/led_video/duck_p1_60.mkv` |
| 95 | `/userdata/led_video/duck_p3_60.mkv` |
| 177 | `/userdata/zxbox/video/split/duck_p4_60.mkv` |

```bash
# 写完后（每台都要做）
sudo sed -i 's/\r$//' /home/linaro/led/start_video.sh /home/linaro/led/autostart_video.sh
bash -n /home/linaro/led/start_video.sh && echo 语法OK
sudo systemctl daemon-reload
sudo systemctl enable led-wall.service
```

### 阶段 E：起播（任意一台、任意时刻，不需要四台一起）

起播基准 T0 是**写死的常量** `1767196800`（2026-01-01 00:00:00 +08:00），四台完全一致。播放器启动时会算出"此刻该播哪一帧"并直接 seek 过去，**内容位置是墙钟的纯函数，与进程何时启动无关**：

```
文件帧 = floor((now - T0) / 16.683ms) mod 61086      # 61086 = 1018.1s × 60fps
seek    = 文件帧 / 60 秒
```

所以单台重启、整机重启、断电恢复，都不需要配合其他三台：

```bash
sudo systemctl restart led-wall.service    # 哪台坏了重启哪台，什么时候都行
```

> 起播前脚本会等 `chronyc tracking` 的 System time 收敛到 5ms 以内（最多 90 秒），避免带着时钟误差起播。
> **时钟误差会 1:1 变成画面误差**：1ms ≈ 0.06 帧，100ms ≈ 6 帧。

> 手动起播（调试用）：`sudo bash /home/linaro/led/start_video.sh <切片> 0 1767196800`，T0 必须用这个常量。

### 阶段 F：验收

每台执行，对照“合格值”：

```bash
b1=$(cat /sys/class/net/eth0/statistics/tx_bytes); p1=$(cat /sys/class/net/eth0/statistics/tx_packets)
sleep 3
b2=$(cat /sys/class/net/eth0/statistics/tx_bytes); p2=$(cat /sys/class/net/eth0/statistics/tx_packets)
echo "Mbps=$(( (b2-b1)*8/3/1000000 )) frames/s=$(( (p2-p1)/3/1081 ))"
echo "governor=$(cat /sys/devices/system/cpu/cpu4/cpufreq/scaling_governor)"
echo "eee=$(sudo ethtool --show-eee eth0 | grep -i 'EEE status')"
echo "err=$(cat /sys/class/net/eth0/statistics/tx_errors) drop=$(cat /sys/class/net/eth0/statistics/tx_dropped)"
echo "进程数=$(ps -eo args | grep -c '^python3 /home/linaro/led/rk_send.py')"
```

相位核对（关键，代替以前的“四台 T0 是否相同”）：

```bash
# 每台取最后一行日志，frame = slot mod 61086（61086 = 总帧数）
journalctl -u led-wall.service -n 1 --no-pager -o cat
# 形如: t=1792986206.459 slot=1545826110 frames=...
# 把四台的 slot 折算到同一个时刻 t_ref：frame - (t - t_ref)/0.016683
# 四台结果应在 ±2 帧以内
```

| 指标 | 合格值 | 实测基线 |
|---|---|---|
| 发送速率 | 758 ± 5 Mbps（60fps） | 758–759 Mbps |
| 实际帧率 | 60 帧/秒 | 60 |
| 发送进程数 | 恰好 1 个 | 1 |
| `tx_errors` / `tx_dropped` | 0 / 0 | 0 / 0 |
| CPU 调频 | `performance` | 2.25–2.35 GHz |
| EEE | `disabled` | disabled |
| 时钟偏差 | < 1 ms | 0.12–0.28 ms |
| 起播相位 | 四台折算后内容帧差 < 2 帧 | 差 0.0 帧（0.5 ms） |
| 循环 | 每 1018.1 s 跨一次片尾，四台同时跨 | 跨片尾只多 1–3 次 catchup，无可见顿挫 |
| 日志 | 起播 4–6 次 catchup，之后基本没有 | 实测 4–8 次/轮 |

最后目视验收：四块画面连续、拼缝无错位、无闪烁、无撕裂。

---

## 4. 问题台账（本次实际踩过的坑）

| # | 现象 | 根本原因 | 处置 | 如何预防 |
|---|---|---|---|---|
| 1 | 某块画面持续闪烁 | 同一台机器上**跑了两个发送进程**，抢同一个千兆口，每帧都追不上只能丢帧 | 停服务、杀掉所有 `rk_send.py` 进程，只保留 1 个 | 起播前必须数进程数 = 1；不要“手动起一次 + 服务又起一次” |
| 2 | 重启后画面变卡/闪、速率只有 380Mbps | **CPU 调频回落成 `ondemand`**（177 空闲仅 600MHz），16.7ms 内发不完 1.58MB，只能每两拍发一帧 | 设 `performance`，并**写进 `start_video.sh`** | 调频以前是手动设的，断电就丢；必须固化到启动脚本 |
| 3 | 自启服务反复重启，`exit-code 127` | `start_video.sh` 是 **Windows CRLF 换行**，bash 把 `true\r` 当命令 | `sed -i 's/\r$//'`，再 `bash -n` 校验 | 任何上传到盒子的脚本都要转 LF 并做语法检查 |
| 4 | 四块内容错乱 / 都像 30fps | 四台 `led-wall.service` 的 ExecStart **全指向同一个 30fps 的 duck_p3.mkv** | 逐台改为各自的 `_60.mkv` 路径 | 部署时按“设备→切片”表逐台核对 ExecStart |
| 5 | 画面一顿一顿、日志满屏 `catchup skip` | 切片帧率与周期**错配**（30fps 文件配 16.683ms 周期，或反之） | 60fps 文件必须配 `CL_VIDEO_PERIOD_MS=16.683` | 换切片时同步改周期，两个参数绑死 |
| 6 | 画面频闪（细密闪烁） | 网卡 **EEE 节能**处于 `enabled - active` | `ethtool --set-eee eth0 eee off`，并关流控/offload | 已写入 `start_video.sh`，每次起播自动执行 |
| 7 | 四块之间轻微不同步、拼缝动作错位 | 无共享时钟 + 各自启动时间不同 | chrony 对时 + 统一 T0 + 墙钟时隙 + 落后超 1 帧跳帧 | 只允许四台一起重启；单台重启必然错开 |
| 8 | `ping` 通但 SSH 22 端口超时 | 设备用户态卡死（sshd 无响应），不是网络问题 | 现场断电重启 | 出现此现象直接断电，不要在 SSH 上反复重试 |
| 9 | 大文件上传中途断流（73MB/161MB） | WiFi 传大文件不稳定 | 在 177 起 `python3 -m http.server 8000`，目标机用 `wget` 拉取 | 大文件统一走 HTTP + md5 校验 |
| 10 | 切出来的 MP4 播放异常 | 该容器切片在本机播放器上坏 | 改用 MKV 封装（坏的留在 `bad_mp4/` 不要用） | 统一 MKV |
| 11 | 本地执行命令报 `PSSecurityException` | Windows PowerShell 执行策略禁止运行 `.ps1`（**与本项目设备无关**） | 改用 Python（paramiko）直接 SSH 执行 | 不要把它误判成设备故障 |
| 12 | 前 3 块同步、第 4 块整体错开 2 分钟 | 起播基准 T0 是**每台各自`date +%s` 算的**：177 是 NTP 服务器，chrony 一上电就 Normal，开机后 3 秒算出 T0；其余三台要等 chrony 跟 177 同步，27 秒后才算。两者刚好跨过偶数分钟边界，T0 整整差 120 秒（7193 帧） | 先改成"开机满 60 秒再算 T0"应急；**最终被 #14 的墙钟相位方案取代** | 任何"各自现场算基准"的方案都有竞态，迟早踩到 |
| 13 | 加了“距边界不足 30 秒就顺延”的护栏后，重启仍然错开 2 分钟 | 护栏只是把分界点从“边界”挪到“边界前 30 秒”；`systemctl restart` 时 177 秒起、其余三台慢 10 秒，刚好分落在分界点两侧 | **删掉护栏** | 判定规则里的任何“阈值”都会变成新的竞态分界点，不要用阈值兜底 |
| 14 | 最终方案：任意一台任意时刻重启都能自动对齐 | 内容位置原本由"进程启动时刻"决定；改成由**墙钟**决定：固定 T0 常量 + 起播时 `-ss` 跳到 `(now-T0)/16.683ms mod 总帧数` 对应的位置 | 改 `rk_send.py`（VideoSource 支持 seek、catchup 用绝对时隙）、`autostart_video.sh`（固定 T0 + 时钟闸门）、客户端 chrony 加快轮询 | 凡是"多机拼屏"，位置必须做成墙钟的纯函数，不能依赖启动时序 |
| 15 | 85 整机重启后比其他三台偏了几帧 | 开机时 RTC 差了 1.04 秒，chrony 步进后又赶上 177 重启，客户端轮询 64–1024 秒导致修正滞后；**时钟误差 1:1 变成画面误差** | 客户端改 `minpoll 4 maxpoll 6`；起播脚本等 System time <5ms 再起播 | 时钟是相位的地基，误差必须 <1ms（≈0.06 帧） |
| 16 | 改成墙钟 seek 后，起播时看着对齐，过十几分钟又散 | **`-ss` 与 `-stream_loop -1` 同时用时，循环不会回到文件开头**（实测跨片尾后 300 帧既不等于开头、也不等于起播点）。每台在自己的起播点附近循环，四台循环区间长度不同，转几圈就越差越远 | 循环改由 `VideoSource` 自己管：首遍 `-ss` 播到片尾，之后每遍都从 0 起播，并提前 120 帧预取解码器消除空档 | 带 `-ss` 的循环必须自己控制，不要依赖 `-stream_loop`；验证要**比像素/帧哈希**，不能只看 slot 数字 |
| 17 | 起播自带 4–9 帧固定偏差 | `base` 取的是“开始发送的时刻”，而 seek 时隙在此前 0.1–0.2 秒算的，两者差的就是解码启动时间 | `base` 改为 seek 所在时隙（`base_slot`），由 catchup 把内容拉到墙钟相位 | 任何“基准时刻”都必须和被控量同源 |
| 18 | 第 3 块（95）整体比其他三块快约 1.1 秒，重启也不消失（2026-10-27 复现） | 95 的 RTC 快了 1.145 秒；服务 09:48:35 起播，chrony 09:48:55 才选到 177 并**把系统时钟步进 −1.144988 秒**。旧闸门查的 `System time` 在未同步时恒为 0，直接放行。时钟一回跳，内容相对墙钟就永久超前 68 帧（1.145/0.016683），而 catchup 只会“向前跳帧”，修不了超前 | 重启该台即恢复；已把闸门改成“`Reference ID=AC1421B1` 且 `Update interval>0` 且偏差 <5ms”才起播，并在 `rk_send.py` 加 resync 兜底 | 判据必须用“真正同步过”的证据（Reference ID / Update interval），不能用未同步时也为 0 的数值 |
| 19 | 时钟被步进后，内容相位永久跑偏且无人干预 | `catchup` 只处理“发送落后”，内容超前时永远不会回拉 | `rk_send.py` 的 `stream()` 增加相位自检：`base+frames-(slot+1)` 超前 >6 帧或落后 >600 帧时，重新定位解码器到 `(slot+1) mod 总帧数`；10 秒内最多纠偏一次 | 只要相位是墙钟的函数，就必须有“墙钟跳变”的兜底路径 |

---

## 5. 日常运维

### 5.1 重启规程

| 场景 | 做法 |
|---|---|
| 正常维护 | 想重启哪台就重启哪台，**不需要凑时间、不需要四台一起**；黑屏约 2–3 秒后自动落回原相位 |
| 单台故障 | 修好后单独重启该台即可，其余三台不用动 |
| 断电恢复 | 无需干预，`led-wall.service` 已 enable，起来后会按墙钟相位自动就位 |
| 换视频 | 四台同时换切片（各台路径不同）→ 逐台重启；只要四个切片的**时长/帧数一致**，相位自动对齐 |

> 重启后核对相位：取四台 `journalctl -u led-wall.service -n 1 --no-pager -o cat` 的最后一行，
> 按 `文件帧 = slot mod 61086` 换算并折算到同一时刻，四台差值应在 ±2 帧（±33ms）以内。
> 若某台偏几十帧，先看它的 `chronyc tracking` 时钟偏差——时钟误差会 1:1 变成画面误差。

### 5.2 巡检（建议每周一次）

```bash
# 每台跑一遍，重点看 4 个数
ps -eo args | grep -c '^python3 /home/linaro/led/rk_send.py'   # 应为 1
cat /sys/class/net/eth0/statistics/tx_errors                   # 应为 0
cat /sys/class/net/eth0/statistics/tx_dropped                  # 应为 0
chronyc tracking | grep 'System time'                          # 应 <1ms
```

### 5.3 症状 → 首查项

| 症状 | 先查什么 | 大概率结论 |
|---|---|---|
| 整块黑屏 | `systemctl status led-wall.service`、进程数 | 服务没起来 / ExecStart 路径不存在 / CRLF |
| 画面闪、抖动 | 进程数是否 >1；`scaling_governor` | 双进程 或 调频回落 |
| 画面一顿一顿 | 周期与切片帧率是否匹配；`catchup` 日志 | 30fps/60fps 错配 |
| 细密频闪 | `ethtool --show-eee eth0` | EEE 被重新打开 |
| 拼缝动作错位 | 先看四台 `chronyc tracking` 的 `System time`，再折算 `slot mod 61086` 比内容帧 | 时钟偏差大（>10ms）或某台 `-ss` 起播异常；单独重启该台即可，不用动其余三台 |
| 某一块整体快/慢约 1 秒（重启后仍存在） | `journalctl -u chrony -b --no-pager \| grep -i stepped`；有 `System clock was stepped by` 就是它 | 该台 RTC 偏差被 chrony 步进（#18）。重启该台恢复；新闸门 + resync 已能自动兜底 |
| 速率只有一半（380Mbps） | `scaling_governor`、`ps` | 调频 ondemand 或重复进程 |

### 5.4 回退到 30fps

```bash
sudo sed -i 's|CL_VIDEO_PERIOD_MS=16.683|CL_VIDEO_PERIOD_MS=33.367|' /home/linaro/led/start_video.sh
# 并把 led-wall.service 的 ExecStart 指回 30fps 切片
sudo systemctl daemon-reload
# 四台一起重启
sudo systemctl restart led-wall.service
```

回退后合格值：约 379 Mbps、30 帧/秒。

---

## 6. 一句话总结

> 60fps 直驱的成败只在四件事：**时钟同源（chrony）**、**四台同刻起播（统一 T0）**、**每台只有一个发送进程**、**CPU 满频且网卡 EEE/offload 全关**。这四项任意一项失守，表现都是“闪、顿、不同步”，且症状高度相似——所以排障顺序就按这四项查。
