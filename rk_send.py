#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RK3588 直驱卡莱特接收卡（第4路，480x1080 @ ~29.4Hz）
用法:
  sudo python3 rk_send.py replay /home/linaro/led/s6f_frame4.pkl 5
  sudo python3 rk_send.py mirror 0
环境变量:
  CL_IFACE=eth0  CL_PERIOD_MS=34  CL_SYNC_PKL=/home/linaro/led/s6f_frame4.pkl
"""
import os
import sys
import time
import pickle
import socket
import subprocess
import threading

IFACE = os.environ.get("CL_IFACE", "eth0")
PERIOD = float(os.environ.get("CL_PERIOD_MS", "34")) / 1000.0
SYNC_PKL = os.environ.get("CL_SYNC_PKL", "/home/linaro/led/s6f_frame4.pkl")

WIDTH, HEIGHT = 480, 1080
FRAME_BYTES = WIDTH * HEIGHT * 3
ROW_LEN = 1461
DST = bytes.fromhex("112233445566")
SRC = bytes.fromhex("222233445566")


def log(*a):
    print(*a, flush=True)


def open_socket():
    s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW)
    s.bind((IFACE, 0))
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8 * 1024 * 1024)
    except OSError:
        pass
    return s


def make_rows():
    rows = []
    for y in range(HEIGHT):
        b = bytearray(ROW_LEN)
        b[0:6] = DST
        b[6:12] = SRC
        b[12] = 0x55
        b[13] = (y >> 8) & 0xFF
        b[14] = y & 0xFF
        b[15] = 0
        b[16] = 0
        b[17] = (WIDTH >> 8) & 0xFF
        b[18] = WIDTH & 0xFF
        b[19] = 0x08
        b[20] = 0x88
        rows.append(b)
    return rows


def load_capture(path):
    with open(path, "rb") as f:
        cap = pickle.load(f)
    pkts = cap["packets"]
    sync = bytearray(pkts[-1][1])
    row_pkts = pkts[:-1]
    if len(row_pkts) != HEIGHT:
        log("警告: 抓包行包数 %d != %d" % (len(row_pkts), HEIGHT))
    raw = bytearray()
    for _, p in row_pkts[:HEIGHT]:
        raw += p[21:21 + WIDTH * 3]
    if len(raw) != FRAME_BYTES:
        raise SystemExit("抓包像素长度异常: %d" % len(raw))
    return bytes(raw), sync


class X11Source(object):
    """用 ffmpeg x11grab 抓 1920x1080，裁最右侧 480 列，输出 bgr24"""

    def __init__(self):
        self.latest = None
        self.lock = threading.Lock()
        self.stop = False
        env = os.environ.copy()
        if not env.get("DISPLAY"):
            env["DISPLAY"] = ":0"
        if not env.get("XAUTHORITY"):
            env["XAUTHORITY"] = "/home/linaro/.Xauthority"
        cmd = [
            "ffmpeg", "-loglevel", "error",
            "-f", "x11grab", "-framerate", "30",
            "-video_size", "1920x1080", "-i", env["DISPLAY"] + ".0",
            "-vf", "crop=480:1080:1440:0",
            "-vsync", "0",
            "-f", "rawvideo", "-pix_fmt", "bgr24", "-",
        ]
        self.errlog = open("/tmp/rk_ffmpeg.log", "wb")
        self.proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=self.errlog, env=env,
            bufsize=FRAME_BYTES)
        self.thread = threading.Thread(target=self._reader, daemon=True)
        self.thread.start()

    @staticmethod
    def _read_exact(f, n):
        parts = []
        got = 0
        while got < n:
            b = f.read(n - got)
            if not b:
                return None
            parts.append(b)
            got += len(b)
        return b"".join(parts)

    def _reader(self):
        f = self.proc.stdout
        while not self.stop:
            buf = self._read_exact(f, FRAME_BYTES)
            if not buf:
                break
            with self.lock:
                self.latest = buf

    def get(self):
        with self.lock:
            return self.latest

    def close(self):
        self.stop = True
        try:
            self.proc.terminate()
        except Exception:
            pass


class VideoSource(object):
    """用 ffmpeg + h264_rkmpp 解码 480x1080 切片，管道反压自然限速"""

    def __init__(self, path, seek=0.0, total=0, prefetch=120):
        # 循环由本类自己管：首遍从 seek 点播到片尾，之后每遍都从文件开头。
        # 不能用 -stream_loop：它与 -ss 同时使用时循环会重新 seek 到起点，实测跑偏。
        self.path = path
        self.total = total
        self.prefetch = prefetch
        self.seek_frames = int(round(seek * 60.0))
        self.pass_start = self.seek_frames
        self.count = 0
        self.next_proc = None
        self.proc = self._spawn(seek)

    def _spawn(self, seek):
        cmd = ["ffmpeg", "-loglevel", "error"]
        if seek > 0:
            cmd += ["-ss", "%.3f" % seek]
        cmd += [
            "-c:v", "h264_rkmpp",
            "-i", self.path,
            "-f", "rawvideo", "-pix_fmt", "bgr24", "-",
        ]
        return subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            bufsize=FRAME_BYTES)

    @staticmethod
    def _read_exact(f, n):
        parts = []
        got = 0
        while got < n:
            b = f.read(n - got)
            if not b:
                return None
            parts.append(b)
            got += len(b)
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
        # 相位纠偏用：丢弃当前解码器，从 seek 秒重新起播
        self.close()
        self.seek_frames = int(round(seek * 60.0))
        self.pass_start = self.seek_frames
        self.count = 0
        self.next_proc = None
        self.proc = self._spawn(seek)

    def close(self):
        for p in (self.proc, self.next_proc):
            try:
                if p is not None:
                    p.terminate()
            except Exception:
                pass


def probe_video(path):
    out = subprocess.check_output([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=r_frame_rate:format=duration",
        "-of", "default=nw=1", path], stderr=subprocess.DEVNULL).decode()
    dur = 0.0
    fps = 0.0
    for line in out.splitlines():
        k, _, v = line.partition("=")
        if k == "duration":
            dur = float(v)
        elif k == "r_frame_rate" and "/" in v:
            a, b = v.split("/")
            fps = float(a) / float(b)
    return dur, fps


def stream(sock, rows, sync, get_frame, seconds, period, sync_epoch=0.0, base_slot=0,
           total=0, fps=60.0, resync=None):
    wall0 = time.time()
    t0 = time.monotonic()
    next_t = t0
    last_report = wall0
    last_resync = t0
    frames = 0
    errs = 0
    slot = 0
    base = base_slot if base_slot else (int((wall0 - sync_epoch) / period) if sync_epoch > 0 else 0)
    slot = base
    log("stream_start wall=%.6f mono=%.6f epoch=%.3f base=%d" % (wall0, t0, sync_epoch, base))
    while True:
        now = time.monotonic()
        if seconds > 0 and now - t0 >= seconds:
            break
        # 时钟被 chrony 步进（前后跳）会让内容相位永久跑偏，catchup 只能往前跳、
        # 修不了"内容超前"，所以这里直接重新定位解码器到墙钟应有的位置。
        if sync_epoch > 0 and total and resync is not None:
            err = base + frames - (slot + 1)
            if (err > 6 or err < -600) and now - last_resync > 10.0:
                target = slot + 1
                resync((target % total) / fps)
                base = target
                frames = 0
                last_resync = now
                log("resync err=%+d 帧, 重新定位到文件帧 %d" % (err, target % total))
        if sync_epoch > 0 and base + frames < slot + 1:
            skip = min(3, slot + 1 - base - frames)
            for _ in range(skip):
                if get_frame() is None:
                    break
            frames += skip
            log("catchup skip=%d slot=%d frames=%d" % (skip, slot, base + frames))
        data = get_frame()
        if data is None:
            time.sleep(0.002)
            if time.monotonic() - t0 > 5 and frames == 0:
                log("未取到画面（抓屏/解码超时）")
                break
            continue

        for y in range(HEIGHT):
            rows[y][21:] = data[y * WIDTH * 3:(y + 1) * WIDTH * 3]

        for b in rows:
            try:
                sock.send(b)
            except OSError:
                errs += 1
                time.sleep(0.0001)
                try:
                    sock.send(b)
                except OSError:
                    pass

        if sync_epoch > 0:
            now_wall = time.time()
            slot = int((now_wall - sync_epoch) / period)
            target_wall = sync_epoch + (slot + 1) * period
            delay = target_wall - now_wall
            if delay > 0:
                time.sleep(delay)
        else:
            next_t += period
            now = time.monotonic()
            if next_t > now:
                time.sleep(next_t - now)
            else:
                next_t = now

        sync[14] = ((base + frames) if sync_epoch > 0 else frames) & 0xFF
        try:
            sock.send(sync)
        except OSError:
            errs += 1

        frames += 1
        now_wall = time.time()
        if now_wall - last_report >= 1.0:
            log("t=%.3f slot=%s frames=%d fps=%.1f errs=%d" % (
                now_wall,
                str(slot) if sync_epoch > 0 else "-",
                frames,
                frames / max(0.001, now_wall - wall0),
                errs))
            last_report = now_wall

    dt = max(0.001, time.monotonic() - t0)
    log("结束 frames=%d avg_fps=%.1f errs=%d" % (frames, frames / dt, errs))


def main():
    if len(sys.argv) < 2:
        log("用法: rk_send.py replay <pkl> [秒] | mirror [秒] | video <切片> [秒]")
        return 1
    mode = sys.argv[1]
    sock = open_socket()
    rows = make_rows()

    if mode == "replay":
        pkl = sys.argv[2]
        seconds = float(sys.argv[3]) if len(sys.argv) > 3 else 5.0
        raw, sync = load_capture(pkl)
        log("回放: %s, 行包=%d, 同步包=%d, %.1fs" % (pkl, len(rows), len(sync), seconds))
        stream(sock, rows, sync, lambda: raw, seconds, PERIOD)
    elif mode == "mirror":
        seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0
        _, sync = load_capture(SYNC_PKL)
        log("镜像: %s 最右侧 480 列, 周期 %.1fms" % (os.environ.get("DISPLAY", ":0"), PERIOD * 1000))
        src = X11Source()
        try:
            stream(sock, rows, sync, src.get, seconds, PERIOD)
        finally:
            src.close()
    elif mode == "video":
        if len(sys.argv) < 3:
            log("用法: rk_send.py video <切片文件> [秒]")
            return 1
        path = sys.argv[2]
        seconds = float(sys.argv[3]) if len(sys.argv) > 3 else 0.0
        _fps = float(os.environ.get("CL_FPS", "0"))
        vperiod = (1.0 / _fps) if _fps > 0 else float(os.environ.get("CL_VIDEO_PERIOD_MS", "33.367")) / 1000.0
        _, sync = load_capture(SYNC_PKL)
        log("视频: %s, 周期 %.3fms" % (path, vperiod * 1000))
        start_epoch = float(os.environ.get("CL_START_EPOCH", "0"))
        start_delay = float(os.environ.get("CL_START_DELAY", "0"))
        if start_delay > 0:
            log("延迟 %.1f 秒后启动" % start_delay)
            time.sleep(start_delay)
        offset = 0.0
        total = 0
        fps = 0.0
        slot_seek = 0
        if start_epoch > 0:
            if time.time() < start_epoch:
                log("等待启动时刻 %.3f" % start_epoch)
                while time.time() < start_epoch:
                    time.sleep(0.001)
            dur, fps = probe_video(path)
            total = int(round(dur * fps))
            slot_seek = int((time.time() - start_epoch) / vperiod)
            slot_seek += int(os.environ.get("CL_FRAME_OFFSET", "0"))
            offset = (slot_seek % total) / fps
            log("墙钟对齐: 片长 %.3fs 帧率 %.3f 总帧 %d 时隙 %d 文件帧 %d 起点 %.3fs"
                % (dur, fps, total, slot_seek, slot_seek % total, offset))
        src = VideoSource(path, offset, total)
        first = src.get()
        if first is None:
            log("视频解码失败: %s" % path)
            src.close()
            return 1
        pending = [first]

        def get_frame():
            if pending:
                return pending.pop()
            return src.get()

        def resync(seek):
            del pending[:]
            src.restart(seek)

        try:
            stream(sock, rows, sync, get_frame, seconds, vperiod, sync_epoch=start_epoch,
                   base_slot=slot_seek, total=total, fps=fps,
                   resync=resync if (total and fps) else None)
        finally:
            src.close()
    else:
        log("未知模式: %s" % mode)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
