# -*- coding: utf-8 -*-
"""生成《诺瓦LED大屏直驱方案汇报.pptx》—— 10 页, 16:9, 微软雅黑"""
from pptx import Presentation
from pptx.util import Inches, Pt
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.enum.shapes import MSO_SHAPE
from pptx.oxml.ns import qn

# ---------- 尺寸常量 ----------
SLIDE_W = 13.333
SLIDE_H = 7.5
MX = 0.55                      # 左右边距
CONTENT_W = SLIDE_W - 2 * MX   # 12.233
FONT = '微软雅黑'

# ---------- 配色 ----------
DARK_BG = '0B1F3A'
DARK_CARD = '14305A'
NAVY = '12315B'
INK = '1B2A3A'
MUTED = '5A6B7C'
CYAN = '00A8E8'
CYAN_D = '0077B6'
RED = 'E63946'
RED_BG = 'FCEBEC'
GREEN = '2A9D8F'
GREEN_BG = 'E8F5F2'
AMBER = 'E9A13B'
LIGHT_BG = 'F5F8FB'
CARD = 'FFFFFF'
BORDER = 'D8E1EA'

prs = Presentation()
prs.slide_width = Inches(SLIDE_W)
prs.slide_height = Inches(SLIDE_H)
BLANK = prs.slide_layouts[6]


def _style_run(run, size, bold, color, italic=False):
    f = run.font
    f.name = FONT
    f.size = Pt(size)
    f.bold = bold
    f.italic = italic
    f.color.rgb = RGBColor.from_string(color)
    rPr = run._r.get_or_add_rPr()
    ea = rPr.find(qn('a:ea'))
    if ea is None:
        ea = rPr.makeelement(qn('a:ea'), {})
        rPr.append(ea)
    ea.set('typeface', FONT)


def L(text, size=14, bold=False, color=INK, align=PP_ALIGN.LEFT, sa=4, sb=0, ls=None, italic=False):
    """一行文本的规格; text 可为 str 或 [(str, dict), ...] 混合样式"""
    return dict(text=text, size=size, bold=bold, color=color,
                align=align, sa=sa, sb=sb, ls=ls, italic=italic)


def write_lines(tf, lines, anchor=MSO_ANCHOR.TOP):
    tf.word_wrap = True
    tf.vertical_anchor = anchor
    for i, ln in enumerate(lines):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = ln['align']
        p.space_after = Pt(ln['sa'])
        p.space_before = Pt(ln['sb'])
        if ln['ls']:
            p.line_spacing = ln['ls']
        runs = ln['text'] if isinstance(ln['text'], list) else [(ln['text'], {})]
        for rtext, ov in runs:
            r = p.add_run()
            r.text = rtext
            _style_run(r, ov.get('size', ln['size']), ov.get('bold', ln['bold']),
                       ov.get('color', ln['color']), ov.get('italic', ln['italic']))


def add_slide(bg=LIGHT_BG):
    s = prs.slides.add_slide(BLANK)
    s.background.fill.solid()
    s.background.fill.fore_color.rgb = RGBColor.from_string(bg)
    return s


def box(s, x, y, w, h, fill=CARD, line=None, line_w=1.0, round_=True, radius=0.06):
    shp = s.shapes.add_shape(
        MSO_SHAPE.ROUNDED_RECTANGLE if round_ else MSO_SHAPE.RECTANGLE,
        Inches(x), Inches(y), Inches(w), Inches(h))
    if round_:
        try:
            shp.adjustments[0] = radius
        except Exception:
            pass
    if fill is None:
        shp.fill.background()
    else:
        shp.fill.solid()
        shp.fill.fore_color.rgb = RGBColor.from_string(fill)
    if line is None:
        shp.line.fill.background()
    else:
        shp.line.color.rgb = RGBColor.from_string(line)
        shp.line.width = Pt(line_w)
    shp.shadow.inherit = False
    return shp


def tb(s, x, y, w, h, lines, anchor=MSO_ANCHOR.TOP):
    t = s.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = t.text_frame
    tf.margin_left = 0
    tf.margin_right = 0
    tf.margin_top = 0
    tf.margin_bottom = 0
    write_lines(tf, lines, anchor)
    return t


def title_bar(s, num, text, accent=CYAN):
    box(s, MX, 0.46, 0.10, 0.62, fill=accent, round_=False)
    tb(s, MX + 0.26, 0.40, CONTENT_W - 1.2, 0.75,
       [L(text, 26, True, NAVY)])
    tb(s, SLIDE_W - 1.15, 0.46, 0.6, 0.6,
       [L(f'{num:02d}', 20, True, 'B9C6D4', align=PP_ALIGN.RIGHT)])


def chip(s, x, y, d, text, fill=CYAN, tcolor='FFFFFF', size=13):
    c = box(s, x, y, d, d, fill=fill, round_=True, radius=0.5)
    tf = c.text_frame
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    write_lines(tf, [L(text, size, True, tcolor, align=PP_ALIGN.CENTER, sa=0)], MSO_ANCHOR.MIDDLE)
    return c


# ================= S1 封面 =================
s = add_slide(DARK_BG)
# 顶部装饰细条
box(s, 0, 0, SLIDE_W, 0.09, fill=CYAN, round_=False)
tb(s, MX, 1.05, CONTENT_W, 0.4,
   [L('技术方案汇报  ·  2026-11-04', 13, False, '7FB8D9')])
tb(s, MX, 1.75, CONTENT_W, 1.7,
   [L('诺瓦 LED 大屏四路 RK3588', 40, True, 'FFFFFF', sa=6),
    L('内核直驱解决方案', 40, True, 'FFFFFF', sa=0)])
tb(s, MX, 3.62, CONTENT_W, 0.6,
   [L('—— 从卡顿闪黑到 60fps 完美呈现', 21, False, CYAN)])
# 四屏拼接示意
sy = 4.85
bw, bh, gap = 0.62, 1.42, 0.12
for i in range(4):
    box(s, MX + i * (bw + gap), sy, bw, bh, fill=DARK_CARD, line=CYAN, line_w=1.25, round_=False)
tb(s, MX + 4 * (bw + gap) + 0.28, sy, 0.5, bh, [L('▶', 18, True, CYAN, align=PP_ALIGN.CENTER, sa=0)], MSO_ANCHOR.MIDDLE)
wide_x = MX + 4 * (bw + gap) + 0.78
box(s, wide_x, sy, 2.52, bh, fill=None, line=CYAN, line_w=1.75, round_=False)
tb(s, wide_x, sy + bh + 0.12, 2.52, 0.35,
   [L('4 × (480×1080) → 1920×1080', 11, False, '7FB8D9', sa=0)])
# 底部标签
tb(s, MX, 6.75, CONTENT_W, 0.4,
   [L('四路帧级同步    ·    内核态直发    ·    全局时隙网格    ·    60.00 fps', 14, False, 'B9C6D4')])

# ================= S2 项目背景 =================
s = add_slide()
title_bar(s, 2, '项目背景')
card_w = (CONTENT_W - 0.4) / 2
# 左卡: 系统现状
c = box(s, MX, 1.45, card_w, 4.15, fill=CARD, line=BORDER)
tb(s, MX + 0.3, 1.7, card_w - 0.6, 0.45, [L('系统现状', 17, True, NAVY)])
tb(s, MX + 0.3, 2.3, card_w - 0.6, 3.1, [
    L([('· 4 台 ', {}), ('RK3588', {'bold': True, 'color': CYAN_D}), (' 发送端（.27 / .28 / .30 / .31）', {})], 14, sa=10, ls=1.15),
    L([('· 每台直驱 ', {}), ('480×1080', {'bold': True, 'color': CYAN_D}), (' 显示区域', {})], 14, sa=10, ls=1.15),
    L([('· 四路横向拼接为 ', {}), ('1920×1080', {'bold': True, 'color': CYAN_D}), (' 诺瓦大屏', {})], 14, sa=10, ls=1.15),
    L('· 接收端：诺瓦接收卡', 14, sa=10, ls=1.15),
])
# 左卡内小图: 4 路 -> 整屏
iy = 4.55
for i in range(4):
    box(s, MX + 0.3 + i * 0.42, iy, 0.32, 0.78, fill='DCE9F5', line=CYAN_D, line_w=1.0, round_=False)
tb(s, MX + 0.3 + 4 * 0.42 + 0.1, iy, 0.4, 0.78, [L('▶', 13, True, CYAN_D, align=PP_ALIGN.CENTER, sa=0)], MSO_ANCHOR.MIDDLE)
box(s, MX + 0.3 + 4 * 0.42 + 0.5, iy, 1.6, 0.78, fill='DCE9F5', line=CYAN_D, line_w=1.25, round_=False)
# 右卡: 项目目标
rx = MX + card_w + 0.4
c = box(s, rx, 1.45, card_w, 4.15, fill=CARD, line=BORDER)
tb(s, rx + 0.3, 1.7, card_w - 0.6, 0.45, [L('项目目标', 17, True, NAVY)])
goals = [('无卡顿', '帧推送节奏稳定，无调度抖动'),
         ('无闪黑', '逐帧连续，60s 检测闪黑为 0'),
         ('四路帧级同步', '四台按同一时隙网格对齐发送'),
         ('60 fps', '满帧率呈现，四台一致')]
gy = 2.32
for i, (g, d) in enumerate(goals):
    chip(s, rx + 0.3, gy + i * 0.78, 0.44, str(i + 1), fill=CYAN_D)
    tb(s, rx + 0.92, gy + i * 0.78 - 0.04, card_w - 1.25, 0.72, [
        L(g, 15, True, INK, sa=2),
        L(d, 11.5, False, MUTED, sa=0),
    ])
tb(s, MX, 5.85, CONTENT_W, 1.0, [
    L('目标：四路直驱在 1920×1080 诺瓦大屏上实现无卡顿、无闪黑、帧级同步的 60fps 呈现。', 13, False, MUTED, ls=1.2)
])

# ================= S3 总体架构 =================
s = add_slide()
title_bar(s, 3, '总体架构')
nodes = [
    ('片源', '视频文件'),
    ('ffmpeg', '硬解'),
    ('nova_video.py', '墙钟对齐'),
    ('nova_desktop', '组包'),
    ('nova_kmod', '内核直发'),
    ('千兆网口', ''),
    ('诺瓦接收卡', ''),
]
nw, nh, ngap = 1.52, 1.42, 0.18
ny = 1.85
for i, (name, sub) in enumerate(nodes):
    x = MX + i * (nw + ngap)
    hot = (name == 'nova_kmod')
    n = box(s, x, ny, nw, nh, fill=(CYAN_D if hot else CARD), line=(None if hot else BORDER))
    lines = [L(name, 12.5, True, 'FFFFFF' if hot else NAVY, align=PP_ALIGN.CENTER, sa=2)]
    if sub:
        lines.append(L(sub, 10, False, 'D6ECF7' if hot else MUTED, align=PP_ALIGN.CENTER, sa=0))
    write_lines(n.text_frame, lines, MSO_ANCHOR.MIDDLE)
    if i < len(nodes) - 1:
        tb(s, x + nw - 0.03, ny, ngap + 0.06, nh,
           [L('▶', 11, True, CYAN, align=PP_ALIGN.CENTER, sa=0)], MSO_ANCHOR.MIDDLE)
# nova_kmod 特性标注
kx = MX + 4 * (nw + ngap)
box(s, kx - 0.35, ny + nh + 0.22, nw + 0.7, 0.62, fill='E8F5FC', line=CYAN, line_w=1.0)
tb(s, kx - 0.35, ny + nh + 0.22, nw + 0.7, 0.62,
   [L('SCHED_FIFO · CPU6 · dev_direct_xmit', 10, True, CYAN_D, align=PP_ALIGN.CENTER, sa=0)],
   MSO_ANCHOR.MIDDLE)
# 全局时隙网格
gy = 4.45
g = box(s, MX, gy, CONTENT_W, 1.5, fill=NAVY)
tb(s, MX + 0.35, gy + 0.2, CONTENT_W - 0.7, 0.45,
   [L('全局时隙网格（四台共用）', 15, True, 'FFFFFF')])
tb(s, MX + 0.35, gy + 0.68, CONTENT_W - 0.7, 0.7, [
    L([('epoch ', {}), ('1767196800', {'bold': True, 'color': CYAN}),
       ('      周期 ', {}), ('16.667 ms', {'bold': True, 'color': CYAN}),
       ('      相位 ', {}), ('4541 µs', {'bold': True, 'color': CYAN})], 15, color='DCE9F5', sa=3),
    L('四台按同一时隙网格对齐发包，实现帧级同步', 11, False, '9FB4CB', sa=0),
])
tb(s, MX, 6.25, CONTENT_W, 0.6,
   [L('发送路径全程内核态：用户态只做组包与排程，逐包发送由 nova_kmod 在内核中完成。', 12.5, False, MUTED)])

# ================= S4 问题一: 卡顿 =================
s = add_slide()
title_bar(s, 4, '问题一：卡顿', accent=RED)
cw = (CONTENT_W - 0.7) / 2
# 左: 症状与根因
c = box(s, MX, 1.55, cw, 4.5, fill=RED_BG, line=RED, line_w=1.25)
tb(s, MX + 0.3, 1.8, cw - 0.6, 0.45, [L('症状与根因', 16, True, RED)])
tb(s, MX + 0.3, 2.42, cw - 0.6, 3.4, [
    L([('现象：', {'bold': True}), ('画面周期性卡顿', {})], 14, sa=10, ls=1.2),
    L([('根因：', {'bold': True}), ('用户态逐包发送，线程调度抖动超过包间隔，发送节奏被打乱', {})], 14, sa=10, ls=1.2),
    L('用户态发送无法保证微秒级的包间隔精度，抖动累积直接表现为画面卡顿。', 12.5, False, MUTED, ls=1.25),
])
# 中间箭头
tb(s, MX + cw, 1.55, 0.7, 4.5, [L('▶', 22, True, CYAN_D, align=PP_ALIGN.CENTER, sa=0)], MSO_ANCHOR.MIDDLE)
# 右: 解决方案
rx = MX + cw + 0.7
c = box(s, rx, 1.55, cw, 4.5, fill=GREEN_BG, line=GREEN, line_w=1.25)
tb(s, rx + 0.3, 1.8, cw - 0.6, 0.45, [L('解决方案：内核模块直发', 16, True, GREEN)])
tb(s, rx + 0.3, 2.42, cw - 0.6, 3.4, [
    L([('· ', {}), ('nova_kmod', {'bold': True, 'color': NAVY}), (' 内核模块接管逐包发送', {})], 14, sa=9, ls=1.2),
    L([('· ', {}), ('SCHED_FIFO', {'bold': True, 'color': NAVY}), (' 实时调度，绑定 ', {}), ('CPU6', {'bold': True, 'color': NAVY})], 14, sa=9, ls=1.2),
    L([('· ', {}), ('dev_direct_xmit', {'bold': True, 'color': NAVY}), (' 内核态直接发包', {})], 14, sa=9, ls=1.2),
    L([('发送抖动 ', {}), ('p99.9 ≤ 50 µs', {'bold': True, 'color': GREEN}), ('，卡顿消除', {})], 14, sa=0, ls=1.2),
])
tb(s, MX, 6.3, CONTENT_W, 0.55,
   [L('结论：把“逐包发送”从用户态搬进内核态，是消除卡顿的根本手段。', 13, True, NAVY)])

# ================= S5 问题二: 闪黑(核心页) =================
s = add_slide()
title_bar(s, 5, '问题二：从左到右闪黑 —— 三层根因链', accent=RED)
cw3 = (CONTENT_W - 0.5) / 3
cy, ch = 1.5, 4.35
causes = [
    ('①', '帧尾簿记越界', CYAN_D, 'E8F1F8', BORDER, [
        L('帧尾 trash 记录越界，污染相邻数据', 13, sa=8, ls=1.25),
        L([('修复：', {'bold': True}), ('trash 延迟释放 + 边界钳制', {})], 13, sa=0, ls=1.25),
    ]),
    ('②', '启动空补发风暴', CYAN_D, 'E8F1F8', BORDER, [
        L('启动阶段空补发判断错误，引发补发风暴', 13, sa=8, ls=1.25),
        L([('修复：', {'bold': True}), ('修正空补发判断条件', {})], 13, sa=0, ls=1.25),
    ]),
    ('③', 'RT 限流（真凶）', RED, RED_BG, RED, [
        L([('sched_rt_runtime_us', {'bold': True}), (' 默认 950000', {})], 13, sa=7, ls=1.25),
        L('每秒强制踢出 RT 任务 50ms → 每秒丢 1 个时隙', 13, sa=7, ls=1.25),
        L([('33ms 断流 → 接收卡丢锁', {'bold': True, 'color': RED}), ('，从左到右闪黑', {})], 13, sa=7, ls=1.25),
        L([('修复：', {'bold': True}), ('关闭 RT 限流，systemd 持久化', {})], 13, sa=0, ls=1.25),
    ]),
]
for i, (num, t, tc, bg, bd, body) in enumerate(causes):
    x = MX + i * (cw3 + 0.25)
    c = box(s, x, cy, cw3, ch, fill=bg, line=bd, line_w=2.0 if i == 2 else 1.0)
    chip(s, x + 0.25, cy + 0.25, 0.5, num, fill=tc, size=17)
    tb(s, x + 0.9, cy + 0.25, cw3 - 1.1, 0.5, [L(t, 15.5, True, tc, sa=0)], MSO_ANCHOR.MIDDLE)
    tb(s, x + 0.28, cy + 1.05, cw3 - 0.56, ch - 1.25, body)
tb(s, MX, 6.15, CONTENT_W, 0.75, [
    L([('结论：三层根因叠加导致闪黑；定位并关闭 RT 限流这一真凶后，', {}),
       ('60s 逐帧检测闪黑 = 0', {'bold': True, 'color': GREEN}), ('。', {})], 13.5, color=NAVY)
])

# ================= S6 问题三: 60fps 黑屏花屏 =================
s = add_slide()
title_bar(s, 6, '问题三：60fps 黑屏花屏', accent=RED)
lw = 5.6
c = box(s, MX, 1.55, lw, 4.5, fill=CARD, line=BORDER)
tb(s, MX + 0.3, 1.8, lw - 0.6, 0.45, [L('根因与解决', 16, True, NAVY)])
tb(s, MX + 0.3, 2.4, lw - 0.6, 3.5, [
    L([('· 根因：', {'bold': True}), ('包间隔 gap 取 14µs 时，单时隙数据仅铺满 ', {}),
       ('90.8%', {'bold': True, 'color': RED}), ('，被诺瓦接收卡拒收 → 黑屏 / 花屏', {})], 13.5, sa=10, ls=1.25),
    L([('· 解决：', {'bold': True}), ('gap 调整为 ', {}), ('15µs', {'bold': True, 'color': GREEN}),
       ('，时隙铺满率达 ', {}), ('97.3%', {'bold': True, 'color': GREEN}), ('，接收卡正常锁存', {})], 13.5, sa=10, ls=1.25),
    L([('· 经验：', {'bold': True}), ('时隙数据需铺满约 ', {}), ('97% 以上', {'bold': True, 'color': CYAN_D}), (' 才被接收卡认可', {})], 13.5, sa=0, ls=1.25),
])
# 右侧铺满率对比条形图
gx = MX + lw + 0.45
gw = CONTENT_W - lw - 0.45
c = box(s, gx, 1.55, gw, 4.5, fill=CARD, line=BORDER)
tb(s, gx + 0.3, 1.8, gw - 0.6, 0.45, [L('单时隙数据铺满率对比', 16, True, NAVY)])
bar_x = gx + 1.55
bar_full = gw - 1.55 - 0.85   # 100% 对应宽度
b1y, b2y, bh2 = 2.75, 4.05, 0.52
tb(s, gx + 0.3, b1y, 1.2, bh2, [L('gap 14µs', 12, True, INK, sa=0)], MSO_ANCHOR.MIDDLE)
box(s, bar_x, b1y, bar_full * 0.908, bh2, fill=RED, round_=False)
tb(s, bar_x + bar_full * 0.908 + 0.08, b1y, 0.8, bh2, [L('90.8%', 13, True, RED, sa=0)], MSO_ANCHOR.MIDDLE)
tb(s, gx + 0.3, b2y, 1.2, bh2, [L('gap 15µs', 12, True, INK, sa=0)], MSO_ANCHOR.MIDDLE)
box(s, bar_x, b2y, bar_full * 0.973, bh2, fill=GREEN, round_=False)
tb(s, bar_x + bar_full * 0.973 + 0.08, b2y, 0.8, bh2, [L('97.3%', 13, True, GREEN, sa=0)], MSO_ANCHOR.MIDDLE)
# 97% 阈值线
thx = bar_x + bar_full * 0.97
box(s, thx, 2.5, 0.025, 2.3, fill=NAVY, round_=False)
tb(s, thx - 0.85, 4.85, 1.8, 0.4, [L('识别阈值 ≈97%', 10.5, True, NAVY, align=PP_ALIGN.CENTER, sa=0)])
tb(s, MX, 6.3, CONTENT_W, 0.55,
   [L('结论：1µs 的 gap 调整（14→15µs）使铺满率越过接收阈值，60fps 黑屏花屏解决。', 13, True, NAVY)])

# ================= S7 60fps 升级实施 =================
s = add_slide()
title_bar(s, 7, '60fps 升级实施')
steps = [
    ('1', '片源预转码', CYAN_D, [
        L([('h264_rkmpp', {'bold': True}), (' 硬解预转码', {})], 13.5, sa=7, ls=1.25),
        L('按四路切片拆分', 13.5, sa=7, ls=1.25),
        L([('每台 ', {}), ('61086 帧 / 约 1GB', {'bold': True, 'color': CYAN_D})], 13.5, sa=7, ls=1.25),
        L([('11 倍速', {'bold': True, 'color': CYAN_D}), (' 转码，离线完成', {})], 13.5, sa=0, ls=1.25),
    ]),
    ('2', '服务切换', GREEN, [
        L('30fps → 60fps 仅修改', 13.5, sa=7, ls=1.25),
        L([('service 中 ', {}), ('3 个环境变量', {'bold': True, 'color': GREEN})], 13.5, sa=7, ls=1.25),
        L('重启服务即生效', 13.5, sa=0, ls=1.25),
    ]),
    ('3', '内核零改动', NAVY, [
        L([('内核模块与发送路径', {'bold': True}), (' 完全不变', {})], 13.5, sa=7, ls=1.25),
        L('升级风险最小', 13.5, sa=7, ls=1.25),
        L('回滚同样只改环境变量', 13.5, sa=0, ls=1.25),
    ]),
]
cw3 = (CONTENT_W - 0.5) / 3
for i, (num, t, tc, body) in enumerate(steps):
    x = MX + i * (cw3 + 0.25)
    c = box(s, x, 1.6, cw3, 4.3, fill=CARD, line=BORDER)
    box(s, x, 1.6, cw3, 0.14, fill=tc, round_=False)
    chip(s, x + 0.28, 1.95, 0.54, num, fill=tc, size=18)
    tb(s, x + 0.98, 1.95, cw3 - 1.2, 0.54, [L(t, 17, True, tc, sa=0)], MSO_ANCHOR.MIDDLE)
    tb(s, x + 0.3, 2.85, cw3 - 0.6, 2.9, body)
tb(s, MX, 6.2, CONTENT_W, 0.6,
   [L('升级路径：预转码切片 → 改 3 个环境变量 → 重启服务，全程不动内核。', 13, True, NAVY)])

# ================= S8 验收指标对比表 =================
s = add_slide()
title_bar(s, 8, '验收指标对比')
rows_data = [
    ('验收指标', '本方案（四路内核直驱）', '卡莱特方案'),
    ('帧率', '60.00 fps，四台一致', '59.94 fps'),
    ('单路带宽 / 丢包', '约 780 Mbps，0 丢包', '758 Mbps'),
    ('发送相位', '恒定 4541 µs', '—'),
    ('60s 逐帧闪黑', '0 次', '—'),
    ('发送抖动 p99.9', '≤ 50 µs', '—'),
    ('起播同步', '全局时隙网格对齐', 'chrony 起播差 0.46–0.82 ms'),
]
tw, th = CONTENT_W, 4.35
gt = s.shapes.add_table(len(rows_data), 3, Inches(MX), Inches(1.6), Inches(tw), Inches(th)).table
gt.columns[0].width = Inches(3.0)
gt.columns[1].width = Inches(5.23)
gt.columns[2].width = Inches(4.0)
for r, row in enumerate(rows_data):
    gt.rows[r].height = Inches(0.62 if r else 0.65)
    for c, val in enumerate(row):
        cell = gt.cell(r, c)
        cell.vertical_anchor = MSO_ANCHOR.MIDDLE
        cell.margin_left = Inches(0.15)
        cell.margin_right = Inches(0.1)
        cell.fill.solid()
        if r == 0:
            cell.fill.fore_color.rgb = RGBColor.from_string(NAVY)
        elif c == 1:
            cell.fill.fore_color.rgb = RGBColor.from_string(GREEN_BG)
        else:
            cell.fill.fore_color.rgb = RGBColor.from_string(CARD if r % 2 else 'EEF3F8')
        tf = cell.text_frame
        if r == 0:
            write_lines(tf, [L(val, 14.5, True, 'FFFFFF', align=PP_ALIGN.CENTER, sa=0)])
        else:
            bold = (c == 1)
            color = NAVY if c == 0 else (GREEN if c == 1 else INK)
            write_lines(tf, [L(val, 13, bold, color, align=PP_ALIGN.CENTER if c else PP_ALIGN.LEFT, sa=0)])
tb(s, MX, 6.25, CONTENT_W, 0.6,
   [L('本方案在帧率、带宽、相位稳定性上全面达到或优于对比方案，起播同步不依赖 chrony。', 13, True, NAVY)])

# ================= S9 自动化验收体系 =================
s = add_slide()
title_bar(s, 9, '自动化验收体系')
lw = 5.9
c = box(s, MX, 1.55, lw, 4.5, fill=CARD, line=BORDER)
tb(s, MX + 0.3, 1.8, lw - 0.6, 0.45, [L('检测链路', 16, True, NAVY)])
chain = [('海康摄像头', '正对大屏实拍'),
         ('RTSP 拉流', '实时取回画面'),
         ('逐帧亮度分析', '对每帧计算亮度'),
         ('闪黑判据', '自动判定闪黑'),
         ('验收报告', '量化输出结果')]
chy = 2.35
for i, (t, d) in enumerate(chain):
    y = chy + i * 0.72
    b = box(s, MX + 0.35, y, 3.6, 0.56, fill='E8F1F8', line=CYAN_D, line_w=1.0)
    write_lines(b.text_frame, [L([(t + '   ', {'bold': True, 'color': NAVY, 'size': 12.5}),
                                  (d, {'color': MUTED, 'size': 11})], 12.5,
                               align=PP_ALIGN.CENTER, sa=0)], MSO_ANCHOR.MIDDLE)
    if i < len(chain) - 1:
        tb(s, MX + 0.35, y + 0.54, 3.6, 0.2, [L('▼', 10, True, CYAN, align=PP_ALIGN.CENTER, sa=0)])
tb(s, MX + 4.2, chy, lw - 4.5, 3.6, [
    L('判据', 13, True, NAVY, sa=6),
    L([('连续 ≥2 帧', {'bold': True, 'color': RED}), (' 亮度 ', {}), ('< 30', {'bold': True, 'color': RED})], 12.5, sa=5, ls=1.2),
    L([('且相邻帧差 ', {}), ('< 4', {'bold': True, 'color': RED})], 12.5, sa=0, ls=1.2),
])
rx = MX + lw + 0.45
rw = CONTENT_W - lw - 0.45
c = box(s, rx, 1.55, rw, 4.5, fill=GREEN_BG, line=GREEN, line_w=1.25)
tb(s, rx + 0.3, 1.8, rw - 0.6, 0.45, [L('验收结论', 16, True, GREEN)])
tb(s, rx + 0.3, 2.5, rw - 0.6, 1.3, [
    L('0', 54, True, GREEN, align=PP_ALIGN.CENTER, sa=2),
    L('60s 逐帧检测闪黑次数', 13, False, INK, align=PP_ALIGN.CENTER, sa=0),
])
tb(s, rx + 0.3, 4.15, rw - 0.6, 1.6, [
    L([('· 全程机器自验，', {}), ('非肉眼估计', {'bold': True})], 13, sa=7, ls=1.25),
    L('· 判据固化在分析脚本中，可重复执行', 13, sa=7, ls=1.25),
    L('· 通过海康摄像头 RTSP 实时取流', 13, sa=0, ls=1.25),
])
tb(s, MX, 6.3, CONTENT_W, 0.55,
   [L('摄像头实拍 + 逐帧亮度判据，让“无闪黑”成为可复现的机器结论。', 13, True, NAVY)])

# ================= S10 运维与回滚 =================
s = add_slide()
title_bar(s, 10, '运维与回滚')
cw3 = (CONTENT_W - 0.5) / 3
ops = [
    ('日常运维', CYAN_D, [
        L('重启四路发送服务：', 13, sa=6, ls=1.25),
        L('systemctl restart nova-wall', 12.5, True, NAVY, sa=6, ls=1.2),
        L('四台逐台执行即可', 13, sa=0, ls=1.25),
    ]),
    ('30fps 回滚', GREEN, [
        L([('仅需修改 service 中 ', {}), ('3 个环境变量', {'bold': True, 'color': GREEN})], 13, sa=6, ls=1.25),
        L('内核零改动，回滚风险低', 13, sa=6, ls=1.25),
        L('与 60fps 升级互为逆操作', 13, sa=0, ls=1.25),
    ]),
    ('关键文件清单', NAVY, [
        L('· nova_kmod.c（内核模块）', 12.5, sa=5, ls=1.2),
        L('· nova_desktop.c（组包）', 12.5, sa=5, ls=1.2),
        L('· nova_video.py（墙钟对齐）', 12.5, sa=5, ls=1.2),
        L('· rt-throttle.conf（RT 限流持久化）', 12.5, sa=5, ls=1.2),
        L('· nova-wall.service', 12.5, sa=0, ls=1.2),
    ]),
]
for i, (t, tc, body) in enumerate(ops):
    x = MX + i * (cw3 + 0.25)
    c = box(s, x, 1.6, cw3, 4.3, fill=CARD, line=BORDER)
    box(s, x, 1.6, cw3, 0.14, fill=tc, round_=False)
    tb(s, x + 0.3, 1.95, cw3 - 0.6, 0.5, [L(t, 17, True, tc)])
    tb(s, x + 0.3, 2.7, cw3 - 0.6, 3.0, body)
tb(s, MX, 6.2, CONTENT_W, 0.6,
   [L('运维简单、回滚可控：全部切换动作收敛在 3 个环境变量上。', 13, True, NAVY)])

OUT = r'o:\发送卡\发送卡\诺瓦LED大屏直驱方案汇报.pptx'
prs.save(OUT)
print('saved:', OUT)
