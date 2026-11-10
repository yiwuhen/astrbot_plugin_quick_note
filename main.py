# -*- coding: utf-8 -*-
"""AstrBot 插件：随手记（quick note）

随口说一句就记住，自动整理：
  - 「记 明天下午3点交报告」——自动解析时间、优先级，收进清单
  - 「清单」——按 已过期/紧急/有时间/随手记 分组排列（优先级优先）
  - 「清单 本周」——按周一到周日排成周视图，今天高亮
  - 接了 AI 后直接说「别忘了周三交作业」「我最近都有啥事」，AI 调工具完成

设计沿用本仓库另外两个插件的分层：
  parse.py      中文时间/优先级解析（纯函数）
  memostore.py  存储结构与排序/周视图分组（纯函数）
  main.py       AstrBot 编排（命令 + AI 工具 + 自动捕捉）
数据落在 data/plugin_data/astrbot_plugin_quick_note/notes.json。
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

_PLUGIN_ID = "astrbot_plugin_quick_note"
_DATA_FILE = ""

# 极老版本 AstrBot 没有 filter.llm_tool，退化为空装饰器，避免整个插件加载失败
_llm_tool = getattr(filter, "llm_tool", None) or (lambda *a, **k: (lambda fn: fn))


def _load_sibling(module: str):
    """按文件路径加载同目录模块，避开热重载 sys.modules 缓存污染。"""
    name = f"{_PLUGIN_ID}.{module}"
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"{module}.py")
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


memostore = _load_sibling("memostore")
parse = _load_sibling("parse")

MemoError = memostore.MemoError

_AUTO_PREFIX = ("记住", "别忘了", "帮我记", "帮我记住", "备忘")

_HELP_TEXT = """📝 随手记 · 命令

【记】
记 买牛奶                  随手一条
记 明天下午3点交报告        自动识别时间
记 周三晚8点开会 紧急        自动识别优先级
记 后天交作业 !!           !! 也可以标紧急

【看】
清单                      按优先级排列（过期/紧急/有时间/随手记）
清单 本周                 本周周视图（今天高亮）
清单 下周                 下周周视图
清单 已完成               最近完成/删除的记录

【整理】
清单 完成 3               给 #3 打勾
清单 删 3                 删掉 #3

💡 接了 AI 后不用命令：直接说「别忘了周五交周报」「我最近都有啥事」。
时间随口写就行：明天/下周三/11月15号/3天后/晚上8点半 都认得。"""


def _sender_id(event) -> str:
    for attr in ("get_sender_id", "get_user_id"):
        fn = getattr(event, attr, None)
        if callable(fn):
            try:
                value = fn()
                if value not in (None, ""):
                    return str(value)
            except Exception:
                pass
    return "unknown"


def _strip_cmd(text: str, *names: str) -> str:
    """剥掉命令词，返回参数部分。"""
    text = (text or "").strip()
    for name in names:
        if not name:
            continue
        if text.lower().startswith(name.lower()):
            rest = text[len(name):]
            if not rest or rest[0] in " \t":
                return rest.strip()
    return text


@register(
    _PLUGIN_ID,
    "WuHen",
    "随手记：随口一句自动记住，按优先级整理成清单，也能排成周视图",
    "0.1.0",
)
class QuickNotePlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}

    # ------------------------------------------------------------ 生命周期

    async def initialize(self):
        global _DATA_FILE
        try:
            path = StarTools.get_data_dir(_PLUGIN_ID)
            if path:
                _DATA_FILE = os.path.join(str(path), "notes.json")
                os.makedirs(str(path), exist_ok=True)
        except Exception as exc:
            logger.warning(f"随手记: 获取数据目录失败（{exc}），退回插件目录")
            fallback = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
            os.makedirs(fallback, exist_ok=True)
            _DATA_FILE = os.path.join(fallback, "notes.json")
        data = self._load()
        total = sum(len((g or {}).get("notes") or []) for g in (data.get("groups") or {}).values())
        logger.info(f"随手记: 已加载 {len(data.get('groups') or {})} 个会话 / {total} 条未完成（{_DATA_FILE}）")

    # ------------------------------------------------------------ 存储

    def _load(self) -> dict:
        if not _DATA_FILE or not os.path.exists(_DATA_FILE):
            return {"groups": {}}
        try:
            with open(_DATA_FILE, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("根节点不是对象")
            data.setdefault("groups", {})
            return data
        except Exception as exc:
            logger.error(f"随手记: 读取 {_DATA_FILE} 失败: {exc}")
            return {"groups": {}}

    def _save(self, data: dict):
        if not _DATA_FILE:
            return
        tmp = f"{_DATA_FILE}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, _DATA_FILE)
        except Exception as exc:
            logger.error(f"随手记: 保存失败: {exc}")

    def _group(self, data: dict, key: str) -> dict:
        groups = data.setdefault("groups", {})
        if key not in groups or not isinstance(groups[key], dict):
            groups[key] = memostore.new_group()
        return memostore.ensure_group_shape(groups[key])

    # ------------------------------------------------------------ 记 / 整理

    def _capture_text(self, key: str, raw: str, uid: str, priority_hint: int | None = None):
        """解析并落一条随记，返回回执文本。"""
        info = parse.parse_note(raw)
        if not info:
            return (
                "没看出要记什么 🤔\n"
                "直接说就行，比如：记 明天下午3点交报告\n"
                "时间、紧急不紧急都会自动认。发「清单」看已记的内容"
            )
        data = self._load()
        group = self._group(data, key)
        priority = priority_hint if priority_hint in (0, 1, 2) else info["priority"]
        try:
            note = memostore.add_note(
                group, info["text"], due=info["due"],
                has_time=info["has_time"], priority=priority, uid=uid,
            )
        except MemoError as exc:
            return f"没记上：{exc}"
        self._save(data)
        return self._captured_reply(note)

    def _captured_reply(self, note: dict) -> str:
        now = datetime.now()
        lines = ["✅ 记下了", "", f"📝 {note['text']}"]
        when = memostore.fmt_due(note, now)
        if when:
            tag = "⏰ 截止" if note.get("has_time") else "📅 日期"
            lines.append(f"{tag}：{when}")
        pri = {2: "🔥 紧急", 0: "🌿 不急"}.get(note.get("priority", 1))
        if pri:
            lines.append(pri)
        lines += ["", f"编号 #{note['id']} · 「清单 完成 {note['id']}」打勾"]
        return "\n".join(lines)

    def _build_list(self, key: str) -> str:
        now = datetime.now()
        data = self._load()
        group = self._group(data, key)
        notes = memostore.sort_notes(
            [n for n in group["notes"] if not n.get("done")], now
        )
        if not notes:
            return (
                "📝 清单是空的，很清爽 ✨\n\n"
                "随口一句就记上：「记 明天交报告」\n"
                "「清单 本周」可以看周视图"
            )
        overdue: list[dict] = []
        urgent: list[dict] = []
        timed: list[dict] = []
        floating: list[dict] = []
        for n in notes:
            due = memostore._due_of(n)
            if due and due < now:
                overdue.append(n)
            elif n.get("priority") == 2:
                urgent.append(n)
            elif due:
                timed.append(n)
            else:
                floating.append(n)

        try:
            n_max = max(3, int(self.config.get("max_display", 10)))
        except Exception:
            n_max = 10
        total = len(notes)
        lines = [f"📝 清单 · {total} 条未完成", ""]
        if overdue:
            lines.append("⚠️ 已过期")
            lines += [f"  {memostore.fmt_line(n, now)}" for n in overdue[:n_max]]
            lines.append("")
        if urgent:
            lines.append("🔥 紧急/重要")
            lines += [f"  {memostore.fmt_line(n, now)}" for n in urgent[:n_max]]
            lines.append("")
        if timed:
            lines.append("📅 有时间")
            lines += [f"  {memostore.fmt_line(n, now)}" for n in timed[:n_max]]
            lines.append("")
        if floating:
            lines.append("📥 随手记")
            lines += [f"  {memostore.fmt_line(n, now)}" for n in floating[:n_max]]
            lines.append("")
        lines.append("「清单 本周」看周视图 ·「清单 完成 编号」打勾")
        return "\n".join(lines).rstrip()

    def _build_week(self, key: str, offset: int = 0) -> str:
        now = datetime.now()
        data = self._load()
        group = self._group(data, key)
        notes = [n for n in group["notes"] if not n.get("done")]
        week_start, week_end = memostore.week_of(now, offset)
        overdue, days, floating = memostore.group_by_day(notes, week_start, now)

        title = "本周" if offset == 0 else ("下周" if offset == 1 else f"+{offset} 周")
        start_s = f"{week_start.month:02d}-{week_start.day:02d}"
        end_d = week_end - timedelta(days=1)
        end_s = f"{end_d.month:02d}-{end_d.day:02d}"
        lines = [f"📅 {title}安排（{start_s} ~ {end_s}）", ""]
        today = now.date()
        day_count = 0
        for i, day_notes in enumerate(days):
            d = week_start.date() + timedelta(days=i)
            head = f"{memostore._WEEK_CN[i]} {d.month:02d}-{d.day:02d}"
            if d == today:
                head += " ← 今天"
            if not day_notes:
                if offset == 0 and d == today:
                    lines.append(head)
                    lines.append("  （今天没有安排）")
                continue
            day_count += len(day_notes)
            lines.append(head)
            for n in day_notes:
                due = memostore._due_of(n)
                mark = "🔥 " if n.get("priority") == 2 else ("🌿 " if n.get("priority") == 0 else "")
                time_desc = f"{due.hour:02d}:{due.minute:02d} " if (due and n.get("has_time")) else ""
                lines.append(f"  · {time_desc}{mark}{n['text']}（#{n['id']}）")
            lines.append("")
        extra = []
        if overdue:
            extra.append("⚠️ 已过期：" + "、".join(f"#{n['id']} {n['text']}" for n in overdue[:8]))
        out_of_week = [n for n in floating if memostore._due_of(n)]
        if out_of_week:
            extra.append("🔭 更远： " + "、".join(f"#{n['id']} {n['text']}" for n in out_of_week[:8]))
        pure_floating = [n for n in floating if not memostore._due_of(n)]
        if pure_floating:
            extra.append("📥 不限时间：" + "、".join(f"#{n['id']} {n['text']}" for n in pure_floating[:10]))
        lines += extra
        if day_count == 0 and not extra:
            return "📅 这一周还没有安排，很闲 ✨\n随口一句就记上：「记 周五交周报」"
        return "\n".join(lines).rstrip()

    def _do_complete(self, key: str, nid, undo: bool = False) -> str:
        data = self._load()
        group = self._group(data, key)
        try:
            if undo:
                done_list = group.get("done") or []
                target = next((n for n in done_list if n["id"] == int(nid)), None)
                if not target:
                    return f"已完成记录里没有 #{nid}。"
                done_list.remove(target)
                target["done"] = False
                target.pop("done_at", None)
                group["notes"].append(target)
                self._save(data)
                return f"↩️ #{nid} 已恢复到清单：{target['text']}"
            note = memostore.complete_note(group, nid)
        except MemoError as exc:
            return str(exc)
        self._save(data)
        remaining = len(group["notes"])
        tail = f"还剩 {remaining} 条未完成" if remaining else "清单清零啦 🎉"
        return f"✔️ 完成：{note['text']}\n{tail}"

    def _do_remove(self, key: str, nid) -> str:
        data = self._load()
        group = self._group(data, key)
        try:
            note = memostore.remove_note(group, nid)
        except MemoError as exc:
            return str(exc)
        self._save(data)
        return f"🗑 已删除 #{note['id']}：{note['text']}"

    def _build_archive(self, key: str) -> str:
        data = self._load()
        group = self._group(data, key)
        done = (group.get("done") or [])[-10:]
        if not done:
            return "还没有完成/删除过的记录。"
        lines = ["🗂 最近处理过的（最多 10 条）", ""]
        for n in reversed(done):
            mark = "✔️" if n.get("done") else "🗑"
            lines.append(f"  {mark} #{n['id']} {n['text']}")
        return "\n".join(lines)

    # ------------------------------------------------------------ 命令

    @filter.command("记", alias={"记一下", "随手记一条"})
    async def cmd_capture(self, event: AstrMessageEvent):
        """随口记一条：记 明天下午3点交报告"""
        raw = _strip_cmd(event.message_str, "记", "记一下", "随手记一条").strip()
        if not raw:
            yield event.plain_result(_HELP_TEXT)
            return
        key = event.unified_msg_origin
        yield event.plain_result(self._capture_text(key, raw, _sender_id(event)))

    @filter.command("清单", alias={"随记", "备忘", "待办", "todo", "我的清单"})
    async def cmd_list(self, event: AstrMessageEvent):
        """清单管理：清单 / 清单 本周 / 清单 完成 3 / 清单 删 3"""
        key = event.unified_msg_origin
        raw = _strip_cmd(event.message_str, "清单", "随记", "备忘", "待办", "todo", "我的清单").strip()
        word = raw.split(maxsplit=1)[0] if raw else ""
        rest = raw[len(word):].strip() if raw else ""

        if word in ("本周", "这周", "这礼拜", "本周安排"):
            yield event.plain_result(self._build_week(key, 0))
            return
        if word in ("下周", "下礼拜"):
            yield event.plain_result(self._build_week(key, 1))
            return
        if word in ("已完成", "历史", "done", "归档"):
            yield event.plain_result(self._build_archive(key))
            return
        if word in ("完成", "做完", "打勾", "done", "ok"):
            if not rest:
                yield event.plain_result("用法：清单 完成 3（编号看「清单」）")
                return
            yield event.plain_result(self._do_complete(key, rest.split()[0]))
            return
        if word in ("删", "删除", "去掉", "rm"):
            if not rest:
                yield event.plain_result("用法：清单 删 3（编号看「清单」）")
                return
            yield event.plain_result(self._do_remove(key, rest.split()[0]))
            return
        yield event.plain_result(self._build_list(key))

    @filter.command("本周", alias={"周视图", "周安排", "week"})
    async def cmd_week(self, event: AstrMessageEvent):
        """本周安排（周视图）：本周 / 本周 下周"""
        arg = _strip_cmd(event.message_str, "本周", "周视图", "周安排", "week").strip()
        offset = 1 if arg in ("下周", "下礼拜", "next") else 0
        yield event.plain_result(self._build_week(event.unified_msg_origin, offset))

    @filter.command("随手记帮助", alias={"速记帮助"})
    async def cmd_help(self, event: AstrMessageEvent):
        """随手记使用帮助"""
        yield event.plain_result(_HELP_TEXT)

    # ------------------------------------------------------------ AI 工具

    def _llm_enabled(self) -> bool:
        return bool(self.config.get("llm_tools", True))

    @_llm_tool(name="quicknote_add")
    async def tool_add(self, event: AstrMessageEvent, content: str, priority: str = ""):
        """记一条随记/备忘/待办到当前会话的清单。用户说「记住…」「别忘了…」「帮我记一下…」「提醒我…」或表达了任何想被记住的事时调用。内容里可以带时间（如「明天下午3点交报告」「周五交周报」「11月15号」「3天后」），时间会被自动识别。

        Args:
            content(string): 要记住的内容，尽量保留用户的原话（含时间描述），如「明天下午3点交报告」「周五买牛奶」
            priority(string): 优先级：紧急/重要 填「high」，不急 填「low」，普通留空
        """
        if not self._llm_enabled():
            yield event.plain_result("AI 记事已在插件配置里关闭，改用「记」命令吧。")
            return
        pri_map = {"high": 2, "紧急": 2, "重要": 2, "low": 0, "不急": 0, "低": 0}
        hint = pri_map.get((priority or "").strip().lower())
        key = event.unified_msg_origin
        try:
            text = self._capture_text(key, content, _sender_id(event), hint)
        except Exception as exc:
            logger.error(f"随手记: AI 记事失败: {exc}")
            text = "记事出错了，改用「记 内容」命令试试。"
        yield event.plain_result(text)

    @_llm_tool(name="quicknote_list")
    async def tool_list(self, event: AstrMessageEvent):
        """列出当前会话的随手记清单（按 已过期/紧急/有时间/随手记 分组）。用户问「我最近都有啥事」「记了什么」「有哪些待办」时调用。"""
        if not self._llm_enabled():
            yield event.plain_result("AI 查询已在插件配置里关闭，改用「清单」命令吧。")
            return
        try:
            text = self._build_list(event.unified_msg_origin)
        except Exception as exc:
            logger.error(f"随手记: AI 查清单失败: {exc}")
            text = "查询出错了，改用「清单」命令试试。"
        yield event.plain_result(text)

    @_llm_tool(name="quicknote_week")
    async def tool_week(self, event: AstrMessageEvent, week_offset: float = 0):
        """按时间排成本周（或下周）的周视图。用户问「这周有什么安排」「下周要干嘛」「周三有什么事」时调用。

        Args:
            week_offset(number): 0 表示本周（默认），1 表示下周
        """
        if not self._llm_enabled():
            yield event.plain_result("AI 查询已在插件配置里关闭，改用「清单 本周」命令吧。")
            return
        try:
            offset = 1 if int(week_offset or 0) >= 1 else 0
            text = self._build_week(event.unified_msg_origin, offset)
        except Exception as exc:
            logger.error(f"随手记: AI 周视图失败: {exc}")
            text = "查询出错了，改用「清单 本周」命令试试。"
        yield event.plain_result(text)

    @_llm_tool(name="quicknote_complete")
    async def tool_complete(self, event: AstrMessageEvent, number: float):
        """给清单里的某条随记打勾（完成）。用户说「那个完成了」「做完了」时，先发编号或按内容对应清单里的 #编号。

        Args:
            number(number): 清单里的条目编号（#号后面的数字）
        """
        if not self._llm_enabled():
            yield event.plain_result("AI 操作已在插件配置里关闭，改用「清单 完成 编号」命令吧。")
            return
        try:
            text = self._do_complete(event.unified_msg_origin, int(number))
        except Exception as exc:
            logger.error(f"随手记: AI 完成失败: {exc}")
            text = "操作出错了，改用「清单 完成 编号」试试。"
        yield event.plain_result(text)

    @_llm_tool(name="quicknote_delete")
    async def tool_delete(self, event: AstrMessageEvent, number: float):
        """删除清单里的某条随记。用户说「把那条删了」「不用记了」时调用，编号见清单。

        Args:
            number(number): 清单里的条目编号（#号后面的数字）
        """
        if not self._llm_enabled():
            yield event.plain_result("AI 操作已在插件配置里关闭，改用「清单 删 编号」命令吧。")
            return
        try:
            text = self._do_remove(event.unified_msg_origin, int(number))
        except Exception as exc:
            logger.error(f"随手记: AI 删除失败: {exc}")
            text = "操作出错了，改用「清单 删 编号」试试。"
        yield event.plain_result(text)

    # ------------------------------------------------------------ 自动捕捉

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """「记住…」「别忘了…」开头的话自动收进清单，不必发命令。"""
        if not self.config.get("auto_capture", True):
            return
        text = (event.message_str or "").strip()
        if not text or text.startswith("/"):
            return
        if len(text) > 60:
            return
        if not text.startswith(_AUTO_PREFIX):
            return
        # 像命令的（如「记 明天…」）交给命令处理
        if text.split(maxsplit=1)[0] in ("记", "记一下", "清单", "随手记", "备忘", "待办"):
            return
        info = parse.parse_note(text)
        if not info:
            return  # 「记住我们上次说的」这种没有可记内容的，不动作
        key = event.unified_msg_origin
        reply = self._capture_text(key, text, _sender_id(event))
        event.stop_event()
        yield event.plain_result(reply)
