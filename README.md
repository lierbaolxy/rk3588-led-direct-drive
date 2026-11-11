# RK3588 直驱诺瓦 LED 大屏（4 机 · 60fps）

用 4 台 RK3588 替代诺瓦 V960 发送卡，每台通过千兆网口**点对点直连**一列诺瓦接收卡（480×1080），四列横向拼成 1920×1080。

本分支只包含诺瓦侧实现，卡莱特侧见 `colorlight-only`，两网合一见 `main`。

## 为什么诺瓦比卡莱特难

卡莱特的行包里有**行号字段**，接收卡按内容认行；诺瓦的行包**没有行号**，接收卡只能靠**包的到达时刻**判断这是第几行。

也就是说，**发送节拍本身就是协议的一部分**。用户态 `sendto` 单次系统调用 20–50µs，而 60fps 需要 15µs/包，根本追不上；`sendmmsg` 整帧提交会让网卡按线速突发，接收卡缓冲溢出直接行错位。这就是最终必须写内核模块的原因。

## 协议要点

| 项 | 值 |
|---|---|
| EtherType | `0xFF00`，纯二层 |
| 目的 MAC | `12:34:56:78:9A:BC`（源 MAC 不校验） |
| 包头 | 43 B = `34×00 + 7×55 + D5 09` |
| 行包 | 43 B 头 + `0x1E` + 1440 B = **1498 B**，**无行号** |
| 场包 | 43 B 头 + `0x2D` + 1028 B，16 帧循环 |
| 像素顺序 | **RGB**（与卡莱特相反） |
| 每帧 | 1080 行包 + 1 场包 = 1081 包 ≈ 1.56 MB |
| 停发行为 | **保留最后一帧**，不会立刻黑屏 |

## 内核发送模块

`nova_kmod.ko` 提供 `/dev/nova_tx`：

- 用户态通过 ioctl 提交整帧的包描述符，模块**预建全部 1081 个 SKB**，放进 5 个帧槽。
- CPU6 上跑 **SCHED_FIFO 内核线程，逐包按绝对时刻忙等**（回环实测调度 p99.9 <1µs）。
- 到点用 **`dev_direct_xmit` 绕过 qdisc** 直接下发；副作用是出口抓不到包（预期行为）。
- **相位锚定**：每帧起点取 `epoch + ceil((now-epoch)/period)*period`，多台锁在同一全局时隙。
- 帧尾用`延迟释放队列`+10ms 边界钳制，避免释放 skb 挤占帧尾空档导致丢锁。
- 断流时用备份帧补发，保持接收卡锁定。

## 目录结构

```
.
├── README.md
├── doc/      协议逆向、4 路验证、内核发送核查
├── src/      内核模块、发送端、播放器、启动脚本
├── third/    外部引用说明（不收录第三方代码）
└── other/    运维与验证工具
```

### src/ 源码

| 文件 | 说明 |
|---|---|
| `nova_kmod.c` / `nova_kmod_uapi.h` | 内核模块与 ioctl 接口 |
| `Makefile` | out-of-tree 模块构建 + 发送端编译 |
| `nova_desktop.c` | 发送端（AF_PACKET 后端 + `NOVA_KMOD=1` 内核后端） |
| `nova_video.py` | 播放器：ffmpeg 解码 → 墙钟选帧 → 追帧/循环 |
| `start_nova_video.sh` / `autostart_nova_video.sh` | 网口调优、调频、时钟闸门、起播 |
| `rt-throttle.conf` | `sched_rt_runtime_us=-1`，防 RT 线程被踢出 CPU |
| `nova_test.c` / `nova_txring.c` / `nova_txring2.c` | 失败路径留证（TX_RING 等） |
| `nova_led.py` / `nova_live.py` / `nova_video_live.py` / `video_send.py` | Windows/Npcap 时期实现 |
| `kmod_backup_20261103/` | 已验证可用的源码快照 |

### other/ 工具

| 文件 | 说明 |
|---|---|
| `rk.py` / `fetch_ko.py` | 批量 SSH、拉取编译产物（凭据走环境变量） |
| `rtsp_*.py` | 摄像头逐帧亮度分析，检测闪黑 |
| `make_probe.py` / `nova_video_probe.py` / `remeasure.py` / `solid_feed.py` | 探针生成、纯色供帧与复测 |
| `test_color.sh` / `test_color2.sh` / `test_geom.sh` / `stop_test_send.sh` | 现场单测脚本 |

### doc/ 文档

`交接说明_诺瓦大屏直驱.md`、`诺瓦单列直驱_4路验证总结.md`、`四路内核发送_当前状态与对齐核查.md`。

### third/ 外部引用

`third/README.md` 记录借鉴过的公开资料与出处，不收录第三方代码。

## 编译

必须用与目标机**完全一致**的内核源码树（Rockchip 5.10.226 定制树）。用 vanilla 源码会编出「insmod 返回 0 但 init 不执行」的假加载模块。

```bash
mkdir -p /tmp/pybin && ln -sf /usr/bin/python3 /tmp/pybin/python
cd /userdata/nuowa/kmod
PATH=/tmp/pybin:$PATH make -C /userdata/nuowa/kernel-build/kernel-ae667fd0b2e0ca9f85e8cdba745ac2a8c621eb65 M=$PWD modules
```

## 部署

```bash
sudo insmod nova_kmod.ko
sudo env NOVA_KMOD=1 NOVA_KMOD_CPU=6 \
    NOVA_CROP_X=480 NOVA_SYNC_EPOCH=1767196800 \
    NOVA_SENDER_FPS=60 NOVA_SENDER_GAP_US=15 \
    NOVA_VIDEO_PERIOD_MS=16.667 \
    /userdata/nuowa/autostart_nova_video.sh /userdata/nuowa/video/p2.mkv
```

验收：`/tmp/nova_video_sender.log` 出现 `内核实际 60.00 fps`、`相位=4541us`、`发送错` 停止增长。

## 硬性约束

1. **数据流必须铺满帧周期**：60fps 用 `gap=15µs`（铺开 16.2ms / 16.667ms）。空窗超过 1–3ms 立刻丢锁，画面定格成竖条纹乱码。
2. **接收卡断电会丢「接受第三方帧流」许可**，必须先由 V960/NovaLCT 初始化一次；只拔线换发送端不失效。
3. **刷新率档位在 NovaLCT 里**：这批卡原配置只认 30Hz，32/35/40/48fps 实测全部异常。
4. `SCHED_FIFO` 忙等必须配 `sched_rt_runtime_us=-1`。
5. 每台只能有一个发送进程。
6. 不要发布局配置包。

## 主要踩坑

| 现象 | 根因 | 解法 |
|---|---|---|
| 全黑 / 竖条纹 / 只有底部一条 | 用户态节拍追不上 15–30µs | 内核模块 + `dev_direct_xmit` |
| `PACKET_TX_RING` 零发包 | 该内核 TX 环路不可用 | 放弃，改 LKM |
| `SO_TXTIME`+ETF 不可用 | 内核未编 `NET_SCHED` | 不再改内核本体 |
| insmod 返回 0 但 init 不执行 | vanilla 源码结构体布局不符 | 必须用 Rockchip 定制树 |
| 三台同时卡死、杀不掉卸不掉 | `submit` 无超时 `wait_event` → D 状态 | 加 12ms 超时 + systemd 自愈 |
| 启动瞬间补发风暴 | `resend` 判空逻辑失效 | 改判 `!resend || !resend_count` |
| 帧尾释放挤占空档 | 成批释放占 0.9ms | 延迟释放 + 10ms 钳制 |
| 帧号对齐但相位漂 | 发送按进程启动时刻累加 | 相位锚定到全局 epoch |
| 补发时接缝错位 | 决策点 8ms 太早 | 收到 3ms |

完整过程见 `doc/诺瓦单列直驱_4路验证总结.md`、`doc/四路内核发送_当前状态与对齐核查.md`，起点见 `doc/交接说明_诺瓦大屏直驱.md`。

## 已知限制

- 低频 D 状态卡死：进程可能卡在 `alloc_skb → drain_all_pages → flush_work`，12ms 超时（只覆盖 `submit` 等帧槽）兜不住，只能整机重启。建议加日志看门狗。
- **触发条件已确认（2026-11-11）**：`.28` 的 `eth0` 掉链近 6 天后发送端进入 `D` 状态，`tx_dropped` 累计 326 亿、`发送错` 23 亿；链路恢复后进程仍未自愈，`systemctl stop` 也无效，只能整机重启。建议在 `carrier` 掉线时主动停服务，避免长时间无效发送把内存回收路径拖死。
- 补发率随负载上升（6–34 次/分），供帧窗口被压缩时接缝会偶发错位。
- 时间源单点：全网只有一个 NTP 主时钟。
