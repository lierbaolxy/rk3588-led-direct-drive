#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""诺瓦接收卡视频直驱：墙钟对齐、自动 seek、追帧和主动循环。
发送节拍由 nova_desktop 内部控制在 60fps，行包按 14µs 均匀铺开（不能突发，否则接收卡丢行）。
"""
import os
import subprocess
import sys
import time

WIDTH, HEIGHT = 480, 1080
FRAME_BYTES = WIDTH * HEIGHT * 3
IFACE = os.environ.get("NOVA_IFACE", "eth0")
SENDER = os.environ.get("NOVA_SENDER", "/userdata/nuowa/nova_desktop")
SYNC_EPOCH = float(os.environ.get("NOVA_SYNC_EPOCH", "1767196800"))
# 发送节拍交给 nova_desktop 内部：帧率与包间隔都从这里下发
SENDER_FPS = float(os.environ.get("NOVA_SENDER_FPS", "60"))
SENDER_GAP_US = os.environ.get("NOVA_SENDER_GAP_US", "14")
# 解码帧率、时隙周期必须与发送帧率一致，否则会持续跳帧、播速失真
OUTPUT_FPS = SENDER_FPS if SENDER_FPS > 0 else 60.0
PERIOD = 1.0 / OUTPUT_FPS
CROP_X = int(os.environ.get("NOVA_CROP_X", "0"))
# 提前把帧送进内核，让四路都在时隙边界之前就绪，避免因晚到几毫秒错过同一个边界
PHASE_GUARD = float(os.environ.get("NOVA_PHASE_GUARD_MS", "12")) / 1000.0


def log(*args):
    print(*args, flush=True)


def probe_video(path):
    out = subprocess.check_output([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=r_frame_rate:format=duration",
        "-of", "default=nw=1", path], stderr=subprocess.DEVNULL).decode()
    duration = 0.0
    fps = 0.0
    for line in out.splitlines():
        key, _, value = line.partition("=")
        if key == "duration":
            duration = float(value)
        elif key == "r_frame_rate" and "/" in value:
            numerator, denominator = value.split("/")
            fps = float(numerator) / float(denominator)
    return duration, fps


class VideoSource:
    """裁剪原片并转成 60fps；首遍从 seek 点起播，后续每遍从第 0 帧开始。"""

    def __init__(self, path, seek=0.0, total=0, prefetch=120, fps=OUTPUT_FPS):
        self.path = path
        self.total = total
        self.prefetch = prefetch
        self.fps = fps
        self.seek_frames = int(round(seek * self.fps))
        self.pass_start = self.seek_frames
        self.count = 0
        self.next_proc = None
        self.proc = self._spawn(seek)

    def _spawn(self, seek):
        cmd = ["ffmpeg", "-loglevel", "error"]
        if seek > 0:
            cmd += ["-ss", "%.3f" % seek]
        cmd += ["-c:v", "h264_rkmpp", "-i", self.path,
                "-vf", "crop=%d:%d:%d:0,fps=%d" % (WIDTH, HEIGHT, CROP_X, OUTPUT_FPS),
                "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
        return subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, bufsize=FRAME_BYTES)

    @staticmethod
    def _read_exact(stream, size):
        parts = []
        got = 0
        while got < size:
            data = stream.read(size - got)
            if not data:
                return None
            parts.append(data)
            got += len(data)
        return b"".join(parts)

    def _remaining(self):
        if not self.total:
            return 1 << 30
        return self.total - (self.pass_start + self.count)

    def get(self):
        data = self._read_exact(self.proc.stdout, FRAME_BYTES)
        if data is None:
            if self.next_proc is None:
                self.next_proc = self._spawn(0.0)
            self.proc = self.next_proc
            self.next_proc = None
            self.pass_start = 0
            self.count = 0
            data = self._read_exact(self.proc.stdout, FRAME_BYTES)
        self.count += 1
        if self.next_proc is None and self._remaining() <= self.prefetch:
            self.next_proc = self._spawn(0.0)
        return data

    def restart(self, seek):
        self.close()
        self.seek_frames = int(round(seek * self.fps))
        self.pass_start = self.seek_frames
        self.count = 0
        self.next_proc = None
        self.proc = self._spawn(seek)

    def close(self):
        for proc in (self.proc, self.next_proc):
            if proc is not None:
                try:
                    proc.terminate()
                except OSError:
                    pass


def main():
    if len(sys.argv) < 2:
        log("用法: nova_video.py <视频文件> [秒数，0=一直播放]")
        return 2
    path = sys.argv[1]
    seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0
    duration, source_fps = probe_video(path)
    total = int(round(duration * OUTPUT_FPS))
    if not total or not source_fps:
        log("无法读取视频时长或帧率")
        return 1

    slot_seek = int((time.time() - SYNC_EPOCH) / PERIOD)
    offset = (slot_seek % total) / OUTPUT_FPS
    log("墙钟对齐: 片长 %.3fs 源帧率 %.3f 输出帧率 %.1f 裁剪X %d 总帧 %d 时隙 %d 文件帧 %d 起点 %.3fs"
        % (duration, source_fps, OUTPUT_FPS, CROP_X, total, slot_seek, slot_seek % total, offset))

    source = VideoSource(path, offset, total, fps=OUTPUT_FPS)
    sender_log = open("/tmp/nova_video_sender.log", "wb")
    sender = subprocess.Popen([SENDER, IFACE, str(WIDTH), str(HEIGHT),
                               "%.0f" % SENDER_FPS, SENDER_GAP_US, "0"],
                              stdin=subprocess.PIPE, stderr=sender_log,
                              bufsize=FRAME_BYTES)
    first = source.get()
    if first is None:
        log("视频解码失败:", path)
        source.close()
        sender.terminate()
        return 1
    pending = [first]

    def get_frame():
        if pending:
            return pending.pop()
        return source.get()

    def resync(seek):
        del pending[:]
        source.restart(seek)

    wall0 = time.time()
    mono0 = time.monotonic()
    last_report = wall0
    last_resync = mono0
    base = slot_seek
    slot = slot_seek
    frames = 0
    log("stream_start wall=%.6f mono=%.6f epoch=%.3f base=%d"
        % (wall0, mono0, SYNC_EPOCH, base))

    # 预填 2 帧：内核有 5 个帧槽，常驻 3 帧缓冲后偶发的解码/传输抖动
    # 都被缓冲吸收，不会触发内核补发（补发＝该列重复上一帧、下一时隙
    # 又跳过一帧，八列拼接后接缝处能看出错位）
    prefilled_ok = 0
    for _ in range(2):
        pf = get_frame()
        if pf is None:
            break
        try:
            sender.stdin.write(pf)
            prefilled_ok += 1
        except BrokenPipeError:
            log("诺瓦发送进程已退出，请检查 /tmp/nova_video_sender.log")
            return 1
    if prefilled_ok:
        sender.stdin.flush()
        frames = prefilled_ok

    try:
        while True:
            now = time.monotonic()
            if seconds > 0 and now - mono0 >= seconds:
                break

            # 1) 先等到本时隙的准备时刻（边界前 PHASE_GUARD），四路都在同一时刻把帧送进内核
            now_wall = time.time()
            slot = int((now_wall - SYNC_EPOCH) / PERIOD)
            target_wall = SYNC_EPOCH + (slot + 1) * PERIOD - PHASE_GUARD
            delay = target_wall - now_wall
            if delay > 0:
                time.sleep(delay)

            # 2) 内容帧号严格等于墙钟时隙号，偏差即时校正（超前就空一拍，落后就丢帧）
            want = slot + 1
            have = base + frames
            if (want - have) > 6 or (want - have) < -600:
                if now - last_resync > 10.0:
                    resync((want % total) / OUTPUT_FPS)
                    base = want
                    frames = 0
                    last_resync = now
                    log("resync err=%+d 帧, 重新定位到文件帧 %d" % (have - want, want % total))
            elif want > have:
                skip = min(3, want - have)
                for _ in range(skip):
                    if get_frame() is None:
                        break
                frames += skip
                log("catchup skip=%d slot=%d frames=%d" % (skip, slot, base + frames))

            if base + frames > want:
                # 内容已超前，本时隙不写帧，等下一个时隙
                continue

            data = get_frame()
            if data is None:
                log("视频解码结束或失败")
                break
            try:
                sender.stdin.write(data)
                sender.stdin.flush()
            except BrokenPipeError:
                log("诺瓦发送进程已退出，请检查 /tmp/nova_video_sender.log")
                return 1
            frames += 1

            if time.time() - last_report >= 1.0:
                now_wall = time.time()
                slot = int((now_wall - SYNC_EPOCH) / PERIOD)
                log("t=%.3f slot=%d content=%d frames=%d fps=%.1f"
                    % (now_wall, slot, (base + frames) % total, frames,
                       frames / max(0.001, now_wall - wall0)))
                last_report = now_wall
    except KeyboardInterrupt:
        log("收到中断")
    finally:
        source.close()
        try:
            sender.stdin.close()
        except (OSError, AttributeError):
            pass
        sender.terminate()
        sender_log.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
