# -*- coding: utf-8 -*-
src = open(r"o:\发送卡\发送卡\nova_video.py", encoding="utf-8").read()

old = """            data = get_frame()
            if data is None:
                log("视频解码结束或失败")
                break"""
new = """            data = get_frame()
            if data is None:
                log("视频解码结束或失败")
                break
            s = sum(data[::997])
            PROBE.write("%.4f %d %.1f\\n" % (time.time(), slot, s / (len(data[::997]) or 1)))
            if frames % 30 == 0:
                PROBE.flush()"""
assert old in src, "anchor not found"
src = src.replace(old, new, 1)

src = src.replace("def log(*args):",
                  "PROBE = open('/tmp/probe_brightness.log', 'w')\n\n\ndef log(*args):", 1)

open(r"o:\发送卡\发送卡\nova_video_probe.py", "w", encoding="utf-8", newline="\n").write(src)
print("probe written")
