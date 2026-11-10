# -*- coding: utf-8 -*-
"""随手记核心逻辑：存储结构、排序、周视图分组。

纯函数实现，不依赖 AstrBot。时间统一存「YYYY-MM-DD HH:MM」字符串，
has_time=False 表示只精确到天（默认按 09:00 排序但展示时不显示时刻）。
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta

PRIORITY_HIGH = 2
PRIORITY_NORMAL = 1
PRIORITY_LOW = 0

_PRIORITY_LABEL = {2: "🔥", 1: "", 0: "🌿"}
_WEEK_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


class MemoError(Exception):
    """可直接展示给用户的错误。"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def new_group() -> dict:
    return {"notes": [], "done": [], "next_id": 1, "created_at": _now()}


def ensure_group_shape(group: dict) -> dict:
    group.setdefault("notes", [])
    group.setdefault("done", [])
    group.setdefault("next_id", max([n.get("id", 0) for n in group["notes"]] + [0]) + 1)
    return group


def add_note(group: dict, text: str, due: datetime | None = None,
             has_time: bool = False, priority: int = 1, uid: str = "") -> dict:
    text = re.sub(r"\s+", " ", str(text or "")).strip()[:80]
    if not text:
        raise MemoError("内容不能为空")
    if priority not in (0, 1, 2):
        priority = 1
    note = {
        "id": int(group.get("next_id", 1)),
        "text": text,
        "due": due.strftime("%Y-%m-%d %H:%M") if due else None,
        "has_time": bool(has_time and due),
        "priority": priority,
        "done": False,
        "created_at": _now(),
        "created_by": str(uid or ""),
    }
    group["notes"].append(note)
    group["next_id"] = note["id"] + 1
    return note


def get_note(group: dict, nid) -> dict | None:
    nid = int(nid)
    for n in group.get("notes", []):
        if n["id"] == nid:
            return n
    return None


def complete_note(group: dict, nid) -> dict:
    note = get_note(group, nid)
    if not note:
        raise MemoError(f"没有 #{nid} 这条随记（发「清单」看编号）")
    group["notes"].remove(note)
    note["done"] = True
    note["done_at"] = _now()
    group.setdefault("done", []).append(note)
    if len(group["done"]) > 100:
        del group["done"][:len(group["done"]) - 100]
    return note


def remove_note(group: dict, nid) -> dict:
    note = get_note(group, nid)
    if not note:
        raise MemoError(f"没有 #{nid} 这条随记（发「清单」看编号）")
    group["notes"].remove(note)
    return note


def _due_of(note: dict) -> datetime | None:
    try:
        return datetime.strptime(note["due"], "%Y-%m-%d %H:%M") if note.get("due") else None
    except (ValueError, TypeError):
        return None


def sort_notes(notes: list[dict], now: datetime) -> list[dict]:
    """过期 > 有时间(近的在前，同级里高优先在前) > 随手记(高优先在前)。"""

    def key(n: dict):
        due = _due_of(n)
        overdue = 0 if (due and due < now) else 1
        bucket = 0 if overdue == 0 else (1 if due else 2)
        return (bucket, -n.get("priority", 1), due or datetime.max)

    return sorted(notes, key=key)


def week_of(today: datetime, offset: int = 0) -> tuple[datetime, datetime]:
    """返回某周的周一 00:00 ~ 下周一 00:00。offset=0 本周，1 下周。"""
    monday = (today - timedelta(days=today.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    ) + timedelta(weeks=offset)
    return monday, monday + timedelta(days=7)


def group_by_day(notes: list[dict], week_start: datetime, now: datetime):
    """把随记分进周视图：返回 (overdue, days, floating)。

    overdue : list[dict] 截止已过且早于本周开始的
    days    : [(date, [note])] 周一到周日，每天的（无时刻的也归到当天）
    floating: list[dict] 没写时间的随手记
    """
    overdue: list[dict] = []
    days: list[list[dict]] = [[] for _ in range(7)]
    floating: list[dict] = []
    for n in notes:
        due = _due_of(n)
        if due is None:
            floating.append(n)
            continue
        if due < week_start:
            overdue.append(n)
            continue
        delta = (due.date() - week_start.date()).days
        if 0 <= delta <= 6:
            days[delta].append(n)
        else:
            floating.append(n)  # 本周之外 -> 归到「其他」
    days = [sorted(d, key=lambda x: (_due_of(x) or now)) for d in days]
    return overdue, days, floating


def fmt_due(note: dict, now: datetime) -> str:
    """一条随记的时间描述：11-11 15:00 / 11-11 / 已过期 2 天。"""
    due = _due_of(note)
    if not due:
        return ""
    base = f"{due.month:02d}-{due.day:02d}"
    if note.get("has_time"):
        base += f" {due.hour:02d}:{due.minute:02d}"
    if due < now:
        hours = int((now - due).total_seconds() // 3600)
        if hours >= 48:
            base += f"（已过 {hours // 24} 天）"
        elif hours >= 1:
            base += f"（已过 {hours} 小时）"
        else:
            base = "（刚刚到期）" + base
    return base


def fmt_line(note: dict, now: datetime) -> str:
    mark = _PRIORITY_LABEL.get(note.get("priority", 1), "")
    when = fmt_due(note, now)
    line = f"#{note['id']} {note['text']}"
    if when:
        line += f"（{when}）"
    if mark:
        line += f" {mark}"
    return line
