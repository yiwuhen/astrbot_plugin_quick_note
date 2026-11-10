# -*- coding: utf-8 -*-
"""随口一句话的解析：时间、优先级、内容清洗。

纯函数、零依赖，方便单元测试。支持的写法（2026 年中文口语实测常用）：

时间：
  今天/今晚/明天/明晚/后天/大后天
  周五 / 本周五 / 下周五 / 下下周三（也支持 星期/礼拜）
  11月15号 / 11月15日 / 15号 / 2026年11月15日 / 11-15
  3天后 / 一周后（简版）
  下午3点 / 晚上8点半 / 中午12点 / 10:30 / 9点15 / 早上7点
优先级：
  紧急/急/重要/尽快/!!  -> 高
  不急/有空时/闲时      -> 低
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

# ---------------------------------------------------------------- 中文数字

_CN_DIGIT = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
             "六": 6, "七": 7, "八": 8, "九": 9}


def cn_to_int(text: str) -> int | None:
    """「三」「十二」「二十三」-> int；解析失败返回 None。"""
    text = (text or "").strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    if text in ("半",):
        return None  # 「半」只在「点半」里有意义，单独不成数
    if text == "十":
        return 10
    if "十" in text:
        left, _, right = text.partition("十")
        tens = _CN_DIGIT.get(left, 1) if left else 1
        ones = _CN_DIGIT.get(right, 0) if right else 0
        if (left and left not in _CN_DIGIT) or (right and right not in _CN_DIGIT):
            return None
        return tens * 10 + ones
    out = 0
    for ch in text:
        if ch not in _CN_DIGIT:
            return None
        out = out * 10 + _CN_DIGIT[ch]
    return out


# ---------------------------------------------------------------- 正则

_WEEKDAY_CH = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6, "末": 6}

# 日期词：相对日
_RE_REL_DAY = re.compile(r"(大后天|大後天|后天|後天|明天|明晚|今天|今日|今晚)")
# 日期词：星期（可带 下下/下/本/这 前缀）
_RE_WEEKDAY = re.compile(
    r"((?:下下|下|本|这)个?)?"
    r"(周|星期|礼拜)"
    r"([一二三四五六日天末])"
)
# 日期词：月日 / 日号 / N天后 / YYYY-MM-DD
_RE_MONTH_DAY = re.compile(r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})[日号]")
_RE_DAY_ONLY = re.compile(r"(?<![\d:：])(\d{1,2})[日号](?![\d分秒])")
_RE_IN_DAYS = re.compile(r"([一两二三四五六七八九十半\d]+)天[之後后]")
_RE_DATE_SLASH = re.compile(r"(?<![\d])(\d{1,2})[-/](\d{1,2})(?![\d])")

# 时间词：时刻
_RE_TIME = re.compile(
    r"(凌晨|清晨|早上|上午|中午|午后|下午|傍晚|晚上|夜里|深夜|晚|早)?"
    r"(\d{1,2}|[一二三四五六七八九十]+)"
    r"[点时:：]"
    r"([0-5]?\d|半|整|[一二三四五]?十[一二三四五六七八九]?)?分?(钟)?"
)

# 优先级
_RE_HIGH = re.compile(r"(紧急|很急|急事|急|尽快|尽快要|重要|马上要|asap|!{2,}|！{2,})", re.IGNORECASE)
_RE_LOW = re.compile(r"(不着急|不急|有空(的时候|时)?|闲时|以后再说|低优先)")

# 开头引导词（随口说的部分，去掉才算内容）
_RE_LEAD = re.compile(
    r"^(?:帮我|请|麻烦你?|bot|机器人)?"
    r"(?:记(?:住|下|一下|个|录|一笔)?|备忘|备注|提醒我|别忘了|别忘记|记得|记)"
    r"[，,：:、\s]?"
)
_RE_REMIND_ME = re.compile(r"^提醒我?[，,、\s]?")
_RE_TRAIL = re.compile(r"[一下吧哈喽呢\s]+$")


def _parse_time_of_day(h_raw: str, m_raw: str | None, meridiem: str | None,
                       evening_hint: bool) -> tuple[int, int] | None:
    """把 时刻/ meridiem 组合成 (hour, minute)，非法返回 None。"""
    h = cn_to_int(h_raw) if not h_raw.isdigit() else int(h_raw)
    if h is None:
        return None
    m = 0
    if m_raw:
        if m_raw == "半":
            m = 30
        elif m_raw == "整":
            m = 0
        else:
            m = cn_to_int(m_raw) if not m_raw.isdigit() else int(m_raw)
            if m is None or m > 59:
                return None
    if meridiem:
        if meridiem in ("凌晨", "清晨", "早上", "上午", "早"):
            if h == 12:
                h = 0
        elif meridiem == "中午":
            if h in (1, 2):
                h += 12  # 中午1点 = 13点
        elif meridiem in ("下午", "午后", "傍晚"):
            if h < 12:
                h += 12  # 下午12点保持 12
        elif meridiem in ("晚上", "夜里", "深夜", "晚"):
            if h < 12:
                h += 12
            elif h == 12:
                h = 0  # 晚上12点 = 0 点
    elif evening_hint and h < 12:
        # 「明晚8点」这种：日期词里带了「晚」，时刻没写 meridiem
        h += 12
    if not (0 <= h <= 23):
        return None
    return h, m


def _resolve_date(base: datetime, kind: str, m) -> tuple["datetime.date", str] | None:
    """把匹配到的日期词解析成 date，返回 (date, 匹配原文)。groups 从 match 对象取。"""
    groups = m.groups()
    today = base.date()
    if kind == "rel":
        word = groups[0]
        delta = {"今天": 0, "今日": 0, "今晚": 0, "明天": 1, "明晚": 1,
                 "后天": 2, "後天": 2, "大后天": 3, "大後天": 3}[word]
        return today + timedelta(days=delta), word
    if kind == "weekday":
        prefix, _, wd_ch = groups
        target = _WEEKDAY_CH[wd_ch]
        wd = today.weekday()
        if prefix and prefix.startswith("下下"):
            d = today + timedelta(days=(7 - wd) + 7 + target)
        elif prefix and prefix.startswith("下"):
            d = today + timedelta(days=(7 - wd) + target)
        elif prefix:  # 本周/这周
            d = today + timedelta(days=target - wd)  # 可以为过去
        else:
            d = today + timedelta(days=(target - wd) % 7)
        return d, m.group(0)
    if kind == "month_day":
        y = int(groups[0]) if groups[0] else base.year
        try:
            d = datetime(y, int(groups[1]), int(groups[2])).date()
        except ValueError:
            return None
        if not groups[0] and d < today:
            d = d.replace(year=d.year + 1)
        return d, m.group(0)
    if kind == "day_only":
        try:
            d = today.replace(day=int(groups[0]))
        except ValueError:
            return None
        if d < today:
            # 已过 -> 下个月
            y, mth = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
            d = d.replace(year=y, month=mth)
        return d, m.group(0)
    if kind == "in_days":
        n = cn_to_int(groups[0]) if not groups[0].isdigit() else int(groups[0])
        if n is None or n < 0 or n > 366:
            return None
        return today + timedelta(days=n), m.group(0)
    if kind == "slash":
        try:
            d = datetime(base.year, int(groups[0]), int(groups[1])).date()
        except ValueError:
            return None
        if d < today:
            y, mth = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
            d = d.replace(year=y, month=mth)
        return d, m.group(0)
    return None


def _find_date(text: str, base: datetime):
    """按优先级找第一个日期词，返回 (date, matched_text, span)。"""
    candidates = [
        ("rel", _RE_REL_DAY),
        ("weekday", _RE_WEEKDAY),
        ("month_day", _RE_MONTH_DAY),
        ("in_days", _RE_IN_DAYS),
        ("day_only", _RE_DAY_ONLY),
        ("slash", _RE_DATE_SLASH),
    ]
    for kind, rx in candidates:
        m = rx.search(text)
        if not m:
            continue
        got = _resolve_date(base, kind, m)
        if got:
            d, shown = got
            return d, shown, m.span()
    return None


def _find_time(text: str, date_word: str | None):
    """找第一个时刻词，返回 (hour, minute, matched_text, span) 或 None。"""
    m = _RE_TIME.search(text)
    if not m:
        return None
    meridiem, h_raw, m_raw = m.group(1), m.group(2), m.group(3)
    got = _parse_time_of_day(h_raw, m_raw, meridiem, bool(date_word and "晚" in date_word))
    if got is None:
        return None
    h, minute = got
    return h, minute, m.group(0), m.span()


def parse_note(text: str, now: datetime | None = None) -> dict | None:
    """解析一条随记。

    返回 {"text": 内容, "due": datetime|None, "has_time": bool,
          "priority": 2高/1普通/0低, "time_raw": 原文时间描述}；无可记内容返回 None。
    """
    now = now or datetime.now()
    raw = (text or "").strip()
    if not raw:
        return None

    # 优先级（先取，再从内容里删掉；「不急」要先于「急」判定，避免误判为高优）
    priority = 1
    if _RE_LOW.search(raw):
        priority = 0
    elif _RE_HIGH.search(raw):
        priority = 2

    content = _RE_LOW.sub("", raw)
    content = _RE_HIGH.sub("", content)

    # 时间
    due = None
    has_time = False
    time_raw = ""
    date_info = _find_date(content, now)
    if date_info:
        d, _shown, d_span = date_info
        date_word = _shown
        t = _find_time(content, date_word)
        if t:
            h, minute, _tt, t_span = t
            due = datetime(d.year, d.month, d.day, h, minute)
            has_time = True
            spans = sorted([d_span, t_span])
        else:
            due = datetime(d.year, d.month, d.day, 9, 0)
            has_time = False
            # 「今晚/明晚」没写时刻 -> 默认晚上 8 点
            if "晚" in date_word and _RE_TIME.search(content) is None:
                due = datetime(d.year, d.month, d.day, 20, 0)
                has_time = True
            spans = [d_span]
        time_raw = "".join(content[s:e] for s, e in spans)
        # 从后往前删除匹配片段
        for s, e in sorted(spans, reverse=True):
            content = content[:s] + content[e:]
    else:
        # 只有时刻没有日期：今天，已过则明天
        t = _find_time(content, None)
        if t:
            h, minute, _tt, t_span = t
            due = now.replace(hour=h, minute=minute, second=0, microsecond=0)
            if due <= now:
                due += timedelta(days=1)
            has_time = True
            time_raw = content[t_span[0]:t_span[1]]
            content = content[:t_span[0]] + content[t_span[1]:]

    # 引导词与零碎
    content = _RE_LEAD.sub("", content)
    content = _RE_REMIND_ME.sub("", content)
    content = _RE_TRAIL.sub("", content)
    content = re.sub(r"\s{2,}", " ", content).strip(" ，,。.、；;！!？?")

    if not content:
        return None
    return {
        "text": content,
        "due": due,
        "has_time": has_time,
        "priority": priority,
        "time_raw": time_raw,
    }
