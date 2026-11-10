# -*- coding: utf-8 -*-
"""随手记图片渲染：把清单 / 周视图画成 PNG 卡片。

只依赖 Pillow，不依赖 AstrBot。找不到 Pillow 或中文字体时 AVAILABLE=False，
调用方（main.py）自动回退纯文本，不会因为出图失败影响聊天功能。
"""

from __future__ import annotations

import glob as _glob
import os
from datetime import datetime, timedelta

try:
    from PIL import Image, ImageDraw, ImageFont

    _PIL_OK = True
except Exception:  # pragma: no cover - 没装 Pillow
    _PIL_OK = False

# 2 倍超采样：逻辑尺寸 × 2 出图，手机上不糊
S = 2

W = 900  # 逻辑宽度
M = 30  # 页边距

BG = "#F5F2EC"
CARD = "#FFFFFF"
INK = "#26221C"
SUB = "#8C8378"
FAINT = "#B5ADA2"
LINE = "#EDE7DC"
ACCENT = "#B85C38"  # 主色：陶土红
RED = "#D6453B"
ORANGE = "#D9822B"
BLUE = "#3F7EBD"
GREEN = "#5C8A5F"
GREY = "#A8A29A"

# 常见中文字体，按平台优先级排列
_FONT_CANDIDATES = (
    # Windows
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyh.ttf",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
    r"C:\Windows\Fonts\Deng.ttf",
    # macOS
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    # Linux（AstrBot 官方镜像 / 常见发行版）
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/wenquanyi/wqy-zenhei/wqy-zenhei.ttc",
)

_HINT = ("msyh", "simhei", "simsun", "deng", "pingfang", "hiragino", "arialuni",
         "cjk", "wqy", "sourcehan", "notosanssc", "notoserifsc", "droidsansfallback")

_font_cache: dict[int, object] = {}
_cjk_path_cache: str | None = None


def _find_cjk_font(explicit: str = "") -> str:
    """找一个能显示中文的字体文件，找不到返回空串。"""
    global _cjk_path_cache
    if explicit and os.path.exists(explicit):
        return explicit
    env = os.environ.get("QUICKNOTE_IMAGE_FONT", "").strip()
    if env and os.path.exists(env):
        return env
    if _cjk_path_cache is not None:
        return _cjk_path_cache

    for path in _FONT_CANDIDATES:
        if os.path.exists(path):
            _cjk_path_cache = path
            return path

    # 兜底扫描（不同发行版路径差异很大）
    roots = ("/usr/share/fonts", "/usr/local/share/fonts", os.path.expanduser("~/.fonts"),
             "/System/Library/Fonts", "/Library/Fonts")
    for root in roots:
        if not os.path.isdir(root):
            continue
        try:
            for cur, _dirs, files in os.walk(root):
                for name in files:
                    low = name.lower()
                    if low.endswith((".ttf", ".ttc", ".otf")) and any(h in low for h in _HINT):
                        found = os.path.join(cur, name)
                        _cjk_path_cache = found
                        return found
        except Exception:
            continue
    _cjk_path_cache = ""
    return ""


def available(font_path: str = "") -> bool:
    """Pillow + 中文字体都就绪才算可用。"""
    if not _PIL_OK:
        return False
    return bool(_find_cjk_font(font_path))


def _font(size: int, font_path: str = ""):
    px = max(8, int(round(size * S)))
    key = (px, font_path)
    cached = _font_cache.get(key)  # type: ignore[arg-type]
    if cached is not None:
        return cached
    path = _find_cjk_font(font_path)
    try:
        f = ImageFont.truetype(path, px)
    except Exception:
        f = ImageFont.load_default()
    _font_cache[key] = f  # type: ignore[index]
    return f


def _ellipsis(text: str, font, max_w: float) -> str:
    text = str(text or "")
    if max_w <= 0:
        return ""
    if font.getlength(text) <= max_w:
        return text
    ell = "…"
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if font.getlength(text[:mid] + ell) <= max_w:
            lo = mid
        else:
            hi = mid - 1
    return (text[:lo] + ell) if lo > 0 else ell


class _Canvas:
    """逻辑坐标绘制，内部自动 ×S。"""

    def __init__(self, width: int, height: int):
        self.w = width
        self.h = height
        self.img = Image.new("RGB", (width * S, height * S), BG)
        self.d = ImageDraw.Draw(self.img)

    def _box(self, box):
        return [int(v * S) for v in box]

    def rect(self, box, fill, radius: int = 0, outline=None, width: int = 1):
        b = self._box(box)
        if radius > 0:
            try:
                self.d.rounded_rectangle(b, radius=radius * S, fill=fill,
                                         outline=outline, width=max(1, int(width * S)))
                return
            except Exception:
                pass
        self.d.rectangle(b, fill=fill, outline=outline, width=max(1, int(width * S)))

    def circle(self, cx, cy, r, fill):
        self.d.ellipse([int((cx - r) * S), int((cy - r) * S),
                        int((cx + r) * S), int((cy + r) * S)], fill=fill)

    def text(self, x, y, s, size=15, color=INK, font_path="", anchor="lm", bold=False):
        f = _font(size, font_path)
        try:
            self.d.text((x * S, y * S), str(s), font=f, fill=color, anchor=anchor,
                        stroke_width=(1 if bold else 0), stroke_fill=color)
        except Exception:
            self.d.text((x * S, y * S), str(s), font=f, fill=color)

    def width_of(self, s, size=15, font_path="", bold=False):
        f = _font(size, font_path)
        w = f.getlength(str(s))
        return (w / S) + (1 if bold else 0)

    def save(self, path: str) -> str:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.img.save(path, format="PNG", optimize=True)
        return path


def _fmt_when(item: dict, now: datetime) -> str:
    """右侧时间标签。"""
    due = item.get("_due")
    if not isinstance(due, datetime):
        return "随时"
    if due < now:
        hours = int((now - due).total_seconds() // 3600)
        if hours >= 48:
            return f"已过 {hours // 24} 天"
        if hours >= 1:
            return f"已过 {hours} 小时"
        return "刚刚到期"
    base = f"{due.month:02d}-{due.day:02d}"
    if item.get("has_time"):
        base += f" {due.hour:02d}:{due.minute:02d}"
    delta_days = (due.date() - now.date()).days
    if delta_days == 0:
        return f"今天 {due.hour:02d}:{due.minute:02d}" if item.get("has_time") else "今天"
    if delta_days == 1:
        return f"明天 {due.hour:02d}:{due.minute:02d}" if item.get("has_time") else "明天"
    return base


def _header(c: _Canvas, title: str, subtitle: str, right: str, y: int, font_path: str) -> int:
    """顶部标题区，返回内容起始 y。"""
    c.text(M, y + 30, title, size=30, color=INK, font_path=font_path, bold=True)
    if right:
        rw = c.width_of(right, size=15, font_path=font_path)
        c.text(W - M, y + 34, right, size=15, color=SUB, font_path=font_path, anchor="rm")
        del rw
    if subtitle:
        c.text(M, y + 66, subtitle, size=15, color=SUB, font_path=font_path)
    return y + 100


def _footer(c: _Canvas, y: int, text: str, font_path: str) -> int:
    c.text(W / 2, y + 18, text, size=13, color=FAINT, font_path=font_path, anchor="mm")
    return y + 40


# ------------------------------------------------------------------ 清单图


def measure_list(buckets: list[dict], max_items: int) -> int:
    rows = sum(min(len(b.get("items") or []), max_items) for b in buckets)
    groups = len([b for b in buckets if (b.get("items") or [])])
    return 24 + 100 + (groups * 34 if groups else 26) + rows * 36 + 46


def render_list_image(buckets: list[dict], now: datetime, out_path: str, *,
                      title: str = "清单", subtitle: str = "", right: str = "",
                      footer: str = "", max_items: int = 8, font_path: str = "") -> str:
    """buckets: [{name, color, items:[{id,text,priority,_due,has_time}]}]"""
    font_path = _find_cjk_font(font_path)
    c = _Canvas(W, measure_list(buckets, max_items) + 56)
    c.rect((0, 0, W, 8), ACCENT)

    y = _header(c, title, subtitle, right, 16, font_path)
    c.rect((M, y - 6, W - M, y - 5), LINE)

    if not any((b.get("items") or []) for b in buckets):
        c.text(W / 2, y + 40, "清单是空的，很清爽", size=18, color=SUB,
               font_path=font_path, anchor="mm")
        y += 80
    for b in buckets:
        items = (b.get("items") or [])
        if not items:
            continue
        color = b.get("color") or GREY
        # 分组标题：小色条 + 名称 + 数量
        c.rect((M, y + 9, M + 4, y + 25), color, radius=2)
        c.text(M + 14, y + 17, b.get("name", ""), size=16, color=color,
               font_path=font_path, bold=True)
        count = str(len(items))
        c.text(M + 20 + c.width_of(b.get("name", ""), size=16, font_path=font_path, bold=True),
               y + 18, f"  {count}", size=13, color=FAINT, font_path=font_path)
        y += 34
        for item in items[:max_items]:
            py = y + 17
            pri = item.get("priority", 1)
            dot = RED if pri == 2 else (GREEN if pri == 0 else GREY)
            c.circle(M + 22, py, 4.5, dot)
            when = _fmt_when(item, now)
            when_w = c.width_of(when, size=13, font_path=font_path) + 18
            text_x = M + 38
            max_w = (W - M - 20) - text_x - when_w
            label = f"#{item.get('id')}  {item.get('text', '')}"
            c.text(text_x, py, _ellipsis(label, _font(15, font_path), max_w * S),
                   size=15, color=INK, font_path=font_path)
            c.text(W - M - 20, py, when, size=13, color=SUB, font_path=font_path, anchor="rm")
            y += 36
        if len(items) > max_items:
            c.text(M + 38, y + 6, f"还有 {len(items) - max_items} 条…",
                   size=13, color=FAINT, font_path=font_path)
            y += 24
        y += 10

    y += 6
    _footer(c, y, footer or "随口一句就记上：记 明天下午3点交报告", font_path)
    return c.save(out_path)


# ------------------------------------------------------------------ 周视图


def measure_week(plan: dict, per_day: int) -> int:
    h = 100 + 26
    for day in plan.get("days") or []:
        items = day.get("items") or []
        h += 44
        h += min(len(items), per_day) * 34
        if len(items) > per_day:
            h += 24
        if not items:
            h += 20
    for key in ("overdue", "later", "floating"):
        if plan.get(key):
            h += 46
    return h + 60


def render_week_image(plan: dict, now: datetime, out_path: str, *,
                      title: str = "本周安排", subtitle: str = "", right: str = "",
                      footer: str = "", per_day: int = 6, font_path: str = "") -> str:
    """plan: {days:[{label,date,today,items:[{id,text,priority,_due,has_time}]}],
              overdue:[], later:[], floating:[]}"""
    font_path = _find_cjk_font(font_path)
    c = _Canvas(W, measure_week(plan, per_day) + 20)
    c.rect((0, 0, W, 8), ACCENT)

    y = _header(c, title, subtitle, right, 16, font_path)
    c.rect((M, y - 6, W - M, y - 5), LINE)

    today_found = False
    for day in plan.get("days") or []:
        items = day.get("items") or []
        is_today = bool(day.get("today"))
        block_h = 44 + min(len(items), per_day) * 34 + (24 if len(items) > per_day else 0)
        if not items:
            block_h += 20
        if is_today:
            c.rect((M - 8, y - 2, W - M + 8, y + block_h + 2), "#FBF3EC", radius=10)
            c.rect((M - 8, y + 6, M - 4, y + block_h - 4), ACCENT, radius=2)
            today_found = True

        # 日期头
        head_color = ACCENT if is_today else INK
        c.text(M + 8, y + 22, day.get("label", ""), size=17, color=head_color,
               font_path=font_path, bold=True)
        c.text(M + 8 + c.width_of(day.get("label", ""), size=17, font_path=font_path, bold=True),
               y + 23, f"  {day.get('date', '')}", size=14, color=SUB, font_path=font_path)
        if is_today:
            c.text(W - M - 8, y + 23, "今天", size=13, color=ACCENT,
                   font_path=font_path, anchor="rm", bold=True)
        y += 40

        if not items:
            c.text(M + 14, y + 10, "空", size=13, color=FAINT, font_path=font_path)
            y += 20
        for item in items[:per_day]:
            py = y + 16
            pri = item.get("priority", 1)
            dot = RED if pri == 2 else (GREEN if pri == 0 else GREY)
            c.circle(M + 20, py, 3.5, dot)
            when = ""
            due = item.get("_due")
            if isinstance(due, datetime) and item.get("has_time"):
                when = f"{due.hour:02d}:{due.minute:02d}"
            when_w = c.width_of(when, size=13, font_path=font_path) + 16
            text_x = M + 34
            max_w = (W - M - 14) - text_x - when_w
            c.text(text_x, py, _ellipsis(f"{item.get('text', '')}  #{item.get('id')}",
                                         _font(15, font_path), max_w * S),
                   size=15, color=INK, font_path=font_path)
            if when:
                c.text(W - M - 14, py, when, size=13, color=SUB, font_path=font_path, anchor="rm")
            y += 34
        if len(items) > per_day:
            c.text(M + 34, y + 5, f"还有 {len(items) - per_day} 条…", size=13,
                   color=FAINT, font_path=font_path)
            y += 24
        y += 6

    # 底部收纳区
    extra = (
        ("已过期", plan.get("overdue") or [], RED),
        ("更远", plan.get("later") or [], BLUE),
        ("不限时间", plan.get("floating") or [], GREY),
    )
    for name, items, color in extra:
        if not items:
            continue
        c.rect((M, y + 8, W - M, y + 40), CARD, radius=8, outline=LINE)
        c.rect((M + 10, y + 18, M + 13, y + 30), color, radius=1)
        c.text(M + 22, y + 24, name, size=13, color=color, font_path=font_path, bold=True)
        joined = "  ".join(f"{i.get('text', '')}" for i in items[:6])
        if len(items) > 6:
            joined += f" 等 {len(items)} 条"
        c.text(M + 32 + c.width_of(name, size=13, font_path=font_path, bold=True),
               y + 24, _ellipsis(joined, _font(13, font_path),
                                 ((W - M - 16) - (M + 32 + c.width_of(name, size=13, font_path=font_path, bold=True))) * S),
               size=13, color=SUB, font_path=font_path)
        y += 46

    if not today_found:
        y += 0
    y += 4
    _footer(c, y, footer or "「清单」看优先级排列 · 随口一句就能加", font_path)
    return c.save(out_path)


# ------------------------------------------------------------------ 课表式周视图

TT_W = 1000          # 课表比清单宽一些，7 列才放得下
TT_M = 26            # 页边距
TT_AXIS = 58         # 左侧时段轴宽度
HEAD_H = 54          # 表头（周一 / 11-10）

CARD_PAD = 8
LINE_H = 17
META_H = 15
MAX_PER_CELL = 4     # 单格最多画几条

# 时段：全天(无时刻) / 上午 / 下午 / 晚上
_SLOTS = (("allday", "全天"), ("morning", "上午"),
          ("afternoon", "下午"), ("evening", "晚上"))

_SLOT_HINT = {"allday": "不限时刻", "morning": "06-12",
              "afternoon": "12-18", "evening": "18-24"}

# 卡片配色：背景, 左侧色条
_CARD_STYLE = {
    2: ("#FCEDE8", RED),
    1: ("#F2F7FC", BLUE),
    0: ("#F1F4EF", GREEN),
}


def _slot_of(item: dict) -> str:
    """把一条随记归到课表的时段。"""
    due = item.get("_due")
    if not isinstance(due, datetime) or not item.get("has_time"):
        return "allday"
    h = due.hour
    if h < 12:
        return "morning"
    if h < 18:
        return "afternoon"
    return "evening"


def _wrap_lines(text: str, font, max_w: float, max_lines: int = 3):
    """按宽度折行，最多 max_lines 行，末行加省略号。"""
    text = str(text or "")
    if not text:
        return [""]
    if max_w <= 0:
        return [text]
    lines: list[str] = []
    cur = ""
    for ch in text:
        if font.getlength(cur + ch) <= max_w:
            cur += ch
        else:
            if cur:
                lines.append(cur)
            cur = ch
    if cur:
        lines.append(cur)
    if len(lines) <= max_lines:
        return lines
    kept = lines[:max_lines]
    tail = kept[-1]
    while tail and font.getlength(tail + "…") > max_w:
        tail = tail[:-1]
    kept[-1] = (tail + "…") if tail else "…"
    return kept


def _timetable_layout(plan: dict, font_path: str):
    """算出每格内容、每行高度，绘制和测量共用一份，避免对不齐。"""
    days = plan.get("days") or []
    col_w = (TT_W - 2 * TT_M - TT_AXIS) / 7
    card_w = col_w - 12
    text_px = (card_w - CARD_PAD * 2 - 5) * S
    f13 = _font(13, font_path)

    cells: dict[tuple, tuple] = {}
    row_h: dict[str, int] = {}
    for slot, _label in _SLOTS:
        tallest = 0
        for di in range(7):
            items = []
            if di < len(days):
                items = [it for it in (days[di].get("items") or []) if _slot_of(it) == slot]
            prepared = []
            total = 0
            for it in items[:MAX_PER_CELL]:
                lines = _wrap_lines(it.get("text", ""), f13, text_px, 3)
                ch = CARD_PAD * 2 + len(lines) * LINE_H + META_H
                prepared.append((it, lines, ch))
                total += ch
            total += max(0, len(prepared) - 1) * 6
            if len(items) > MAX_PER_CELL:
                total += 16
            cells[(slot, di)] = (prepared, len(items))
            tallest = max(tallest, total)
        row_h[slot] = max(52, tallest + 14)
    return col_w, card_w, cells, row_h, text_px


def measure_timetable(plan: dict, font_path: str = "") -> int:
    fp = _find_cjk_font(font_path)
    _cw, _card_w, _cells, row_h, _tw = _timetable_layout(plan, fp)
    h = 16 + 100 + HEAD_H + sum(row_h.values()) + 14
    for key in ("overdue", "later", "floating"):
        if plan.get(key):
            h += 46
    return h + 58


def _draw_extras(c: _Canvas, y: int, plan: dict, font_path: str, width: int) -> int:
    """底部收纳：已过期 / 更远 / 不限时间。"""
    extra = (
        ("已过期", plan.get("overdue") or [], RED),
        ("更远", plan.get("later") or [], BLUE),
        ("不限时间", plan.get("floating") or [], GREY),
    )
    for name, items, color in extra:
        if not items:
            continue
        c.rect((TT_M, y + 8, width - TT_M, y + 40), CARD, radius=8, outline=LINE)
        c.rect((TT_M + 10, y + 18, TT_M + 13, y + 30), color, radius=1)
        c.text(TT_M + 22, y + 24, name, size=13, color=color, font_path=font_path, bold=True)
        label_w = c.width_of(name, size=13, font_path=font_path, bold=True)
        joined = "  ".join(f"{i.get('text', '')}" for i in items[:6])
        if len(items) > 6:
            joined += f" 等 {len(items)} 条"
        c.text(TT_M + 32 + label_w, y + 24,
               _ellipsis(joined, _font(13, font_path),
                         ((width - TT_M - 16) - (TT_M + 32 + label_w)) * S),
               size=13, color=SUB, font_path=font_path)
        y += 46
    return y


def render_timetable_image(plan: dict, now: datetime, out_path: str, *,
                           title: str = "本周课表", subtitle: str = "", right: str = "",
                           footer: str = "", font_path: str = "") -> str:
    """课表式周视图：7 列（周一~周日）× 4 行（全天/上午/下午/晚上）网格。

    plan 结构与 render_week_image 相同。
    """
    font_path = _find_cjk_font(font_path)
    col_w, card_w, cells, row_h, _tw = _timetable_layout(plan, font_path)
    days = plan.get("days") or []
    c = _Canvas(TT_W, measure_timetable(plan, font_path) + 16)
    c.rect((0, 0, TT_W, 8), ACCENT)

    y = _header(c, title, subtitle, right, 16, font_path)
    c.rect((TT_M, y - 6, TT_W - TT_M, y - 5), LINE)

    grid_x = TT_M + TT_AXIS
    grid_w = TT_W - TT_M - grid_x
    body_top = y + HEAD_H

    # ---- 表头：周一~周日 ----
    for di in range(7):
        cx = grid_x + di * col_w
        is_today = bool(di < len(days) and days[di].get("today"))
        if is_today:
            c.rect((cx, y - 4, cx + col_w, body_top), "#FBF1EA")
        label = days[di].get("label", "") if di < len(days) else ""
        date = days[di].get("date", "") if di < len(days) else ""
        c.text(cx + col_w / 2, y + 20, label, size=15, color=(ACCENT if is_today else INK),
               font_path=font_path, anchor="mm", bold=True)
        c.text(cx + col_w / 2, y + 40, date, size=11,
               color=(ACCENT if is_today else FAINT), font_path=font_path, anchor="mm")
        if is_today:
            c.circle(cx + col_w / 2, y + 8, 2.6, ACCENT)

    # ---- 网格线 ----
    total_h = sum(row_h[_s] for _s, _ in _SLOTS)
    c.rect((grid_x, body_top, grid_x + grid_w, body_top + total_h), CARD, radius=0)
    yy = body_top
    row_top = {}
    for slot, _label in _SLOTS:
        row_top[slot] = yy
        c.rect((TT_M, yy, TT_W - TT_M, yy + 1), LINE)
        yy += row_h[slot]
    c.rect((TT_M, yy, TT_W - TT_M, yy + 1), LINE)
    # 竖线（含今天列的背景块，先铺色再画线）
    for di in range(8):
        lx = grid_x + di * col_w
        c.rect((lx, body_top, lx + 1, body_top + total_h), LINE)
    # 今天整列淡色（在卡片下方、网格线之上重画会盖线，这里用极淡色放在卡片前）
    for di in range(7):
        if di < len(days) and days[di].get("today"):
            cx = grid_x + di * col_w
            c.rect((cx + 1, body_top + 1, cx + col_w - 1, body_top + total_h - 1), "#FDF8F4")

    # ---- 左侧时段轴 ----
    for slot, label in _SLOTS:
        ry = row_top[slot] + row_h[slot] / 2
        c.text(TT_M + TT_AXIS / 2 - 4, ry - 5, label, size=13, color=SUB,
               font_path=font_path, anchor="mm", bold=True)
        c.text(TT_M + TT_AXIS / 2 - 4, ry + 11, _SLOT_HINT[slot], size=9,
               color=FAINT, font_path=font_path, anchor="mm")

    # ---- 格子内容 ----
    for slot, _label in _SLOTS:
        base_y = row_top[slot]
        for di in range(7):
            prepared, total_items = cells.get((slot, di), ([], 0))
            cx = grid_x + di * col_w + 6
            cy = base_y + 7
            for item, lines, ch in prepared:
                pri = item.get("priority", 1)
                bg, bar = _CARD_STYLE.get(pri, _CARD_STYLE[1])
                due = item.get("_due")
                overdue = isinstance(due, datetime) and due < now
                if overdue:
                    bg, bar = "#FDEEEC", RED
                c.rect((cx, cy, cx + card_w, cy + ch), bg, radius=6)
                c.rect((cx + 3, cy + 4, cx + 5, cy + ch - 4), bar, radius=1)
                ty = cy + CARD_PAD + LINE_H / 2 - 1
                for line in lines:
                    c.text(cx + CARD_PAD + 4, ty, line, size=13, color=INK,
                           font_path=font_path)
                    ty += LINE_H
                meta = f"#{item.get('id')}"
                if isinstance(due, datetime) and item.get("has_time"):
                    meta += f"  {due.hour:02d}:{due.minute:02d}"
                if overdue:
                    meta += "  已过期"
                c.text(cx + CARD_PAD + 4, cy + ch - CARD_PAD - 2, meta, size=10,
                       color=(RED if overdue else FAINT), font_path=font_path)
                cy += ch + 6
            if total_items > MAX_PER_CELL:
                c.text(cx + 4, cy + 4, f"还有 {total_items - MAX_PER_CELL} 条…",
                       size=10, color=FAINT, font_path=font_path)

    y = body_top + total_h + 14
    y = _draw_extras(c, y, plan, font_path, TT_W)
    y += 6
    _footer(c, y, footer or "随手记 · 说「记 周三晚8点开会」就能排进来", font_path)
    return c.save(out_path)
