# RK3588 直驱卡莱特 LED 大屏（4 机 · 60fps）

用 4 台 RK3588（Debian 11）替代卡莱特 S6F 发送卡，每台通过板载千兆网口**点对点直连**一条接收卡链，各驱动 480×1080 一段，四段横向拼成 1920×1080。

本分支只包含卡莱特侧的实现，诺瓦侧见 `nova-only`，两网合一见 `main`。

## 效果

| 指标 | 实测 |
|---|---|
| 帧率 | 60 fps（周期 16.683 ms） |
| 单路带宽 | 758–759 Mbps |
| tx_errors / tx_dropped | 0 / 0 |
| 四台启动差 | 0.46–0.82 ms |
| 时钟偏差 | 3–7 µs |

## 协议要点

| 项 | 值 |
|---|---|
| EtherType | `0x55xx`，纯二层，不用 IP |
| 目的 / 源 MAC | `11:22:33:44:55:66` / `22:22:33:44:55:66` |
| 行包 | 1461 B = 14 以太头 + 7 协议头 + 1440 B 像素 |
| 行号 | **包内偏移 13–14**（这是它比诺瓦好驱动的原因） |
| 同步包 | 1036 B，帧尾发送，第 14 字节逐帧递增 |
| 像素顺序 | **BGR** |
| 每帧 | 1080 行包 + 1 同步包 = 1081 包 ≈ 1.58 MB |

## 文件

| 文件 | 说明 |
|---|---|
| `rk_send.py` | 生产发送端（AF_PACKET），RK3588 上运行 |
| `start_video.sh` | 网口调优（关 EEE/offload/流控）+ 满频 + 绑核 + 起播 |
| `autostart_video.sh` | 时钟闸门 + 固定 T0 + 墙钟 seek |
| `led-wall.service` | systemd 单元，开机自启、异常重启 |
| `analyze_colorlight.py` | 抓包分析：EtherType、行号、包长、帧率 |
| `analyze_sync.py` | 定位同步包中逐帧变化的字节 |
| `extract_frame.py` / `render_frame.py` | 提取一帧并还原图片，验证像素格式 |
| `replay_frame.py` / `stream_frame.py` / `send_image.py` | 原样回放 / 持续循环 / 任意图片 |
| `mirror_screen.py` | 实时镜像屏幕（stretch / letterbox / slice） |
| `colorlight_probe.py` | 发送 0x07 发现包（本卡实测不回复，可跳过） |
| `fix_nic.ps1` | Windows 侧关 EEE/ULPMode/流控 |
| `test_pattern.png` | 480×1080 四色测试图 |

## 部署

1. 四台 `eth0` 各一根网线直连接收卡 IN 口，**不过交换机**；管理走 `wlan0` 或板载 `eth1`。
2. `.177` 起 chrony 本地时钟（`local stratum 10`），其余三台指向它。
3. 切片按列切成 480×1080 并重编码为 60fps（`h264_rkmpp`，MKV 封装）。
4. 安装服务：

```bash
sudo cp led-wall.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now led-wall.service
```

5. 验收：

```bash
b1=$(cat /sys/class/net/eth0/statistics/tx_bytes); sleep 3
b2=$(cat /sys/class/net/eth0/statistics/tx_bytes)
echo "Mbps=$(( (b2-b1)*8/3/1000000 ))"
cat /sys/class/net/eth0/statistics/tx_errors
```

合格：约 758 Mbps、`tx_errors` 为 0、日志 `fps=60.0 errs=0`。

## 关键约束

1. 不要发送 `0x02` 布局配置包，会覆盖 LEDSetting 里的箱体映射。
2. 每台只能有 **1 个**发送进程，两个进程抢同一网口必然闪烁。
3. CPU 必须 `performance`（断电会丢，已写进启动脚本）。
4. 网卡 EEE、gro/gso/tso、流控必须关闭，否则频闪或丢包。
5. 切片帧率与 `CL_VIDEO_PERIOD_MS` 必须绑定：60fps ↔ 16.683 ms。
6. 脚本必须是 LF 换行，CRLF 会让 systemd 报 `exit 127`。
7. 内容位置做成墙钟的纯函数（固定 `T0=1767196800`），任意时刻重启都能自动归位。

## 主要踩坑

| 现象 | 根因 | 解法 |
|---|---|---|
| 入队失败 | Npcap 队列容量漏算每包 512B 外挂开销 | 容量加 512/包 |
| 设 50fps 只跑 47.6 | gap 向上取整导致铺开超周期 | 向下取整 |
| 某块持续闪 | 同机两个发送进程 | 起播前确认进程数 = 1 |
| 重启后只有 380Mbps | 调频回落 ondemand | performance 写进脚本 |
| 自启 exit 127 | 脚本 CRLF | 转 LF + `bash -n` |
| 某块整体偏 1.1 秒 | RTC 偏差被 chrony 步进，内容永久超前 | 闸门查 `Reference ID` + 偏差 <5ms |
| 十分钟后失步 | `-ss` 与 `-stream_loop` 同用不回文件头 | 循环自己管，每遍从 0 起播 |

完整台账见 `RK3588四屏60fps直驱部署SOP.md`（19 条），方案细节见 `RK3588四机直驱LED大屏解决方案.md`，协议逆向见 `协议分析与回放说明.md`。
