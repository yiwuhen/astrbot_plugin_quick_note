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

# WebUI 可视化视图需要较新版本 AstrBot（astrbot.api.web）；旧版本自动降级，
# 面板相关接口不注册，聊天命令与 AI 工具照常工作。
try:
    from astrbot.api.web import error_response, json_response
    from astrbot.api.web import request as web_request

    _WEB_AVAILABLE = True
except Exception:  # pragma: no cover - 老版本 AstrBot
    _WEB_AVAILABLE = False
    web_request = None

    def json_response(data, **kwargs):
        return data

    def error_response(message, status_code=400):
        return {"status": "error", "message": message, "status_code": status_code}


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

# 图片渲染依赖 Pillow + 中文字体；没有就自动回退纯文本
try:
    render = _load_sibling("render")
    _RENDER_AVAILABLE = render.available()
except Exception as exc:  # pragma: no cover
    render = None
    _RENDER_AVAILABLE = False
    logger.warning(f"随手记: 图片渲染不可用（{exc}），清单/周视图回退纯文本")

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
清单 图                   上面的清单做成图片卡片发给你
清单 本周                 本周周视图（今天高亮）
清单 本周 图              周视图做成课表图片（7列 × 时段）
本周 / 本周 图            同上（「本周 下周图」看下周课表）
清单 已完成               最近完成/删除的记录

【整理】
清单 完成 3               给 #3 打勾
清单 删 3                 删掉 #3

💡 接了 AI 后不用命令：直接说「别忘了周五交周报」「我最近都有啥事」。
时间随口写就行：明天/下周三/11月15号/3天后/晚上8点半 都认得。
🖼 跟 AI 说「发张图看看」，清单和周安排会做成图片卡片发出来。

🖥 WebUI 里点开本插件的详情页，有「数据面板」：可视化查看/编辑所有会话
   的记录、按周查看安排、直接改插件配置。"""


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
    "0.4.1",
)
class QuickNotePlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        # 注意不能用 `config or {}`：空配置时会把 AstrBotConfig 对象换成普通 dict，丢失 save_config
        self.config = config if config is not None else {}
        self._register_web_apis()

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
        self._last_cleanup = datetime.now().timestamp()
        self._cleanup_images()
        data = self._load()
        total = sum(len((g or {}).get("notes") or []) for g in (data.get("groups") or {}).values())
        render_state = "开" if self._image_enabled() else "关"
        logger.info(f"随手记: 已加载 {len(data.get('groups') or {})} 个会话 / {total} 条未完成"
                    f"（{_DATA_FILE}，出图 {render_state}，渲染 {'可用' if _RENDER_AVAILABLE else '不可用'}）")

    # ------------------------------------------------------------ 图片输出

    def _image_dir(self) -> str:
        return os.path.join(os.path.dirname(_DATA_FILE) or ".", "images")

    def _image_enabled(self) -> bool:
        return _RENDER_AVAILABLE and bool(self.config.get("image_output", True))

    def _image_max_items(self) -> int:
        try:
            return min(20, max(3, int(self.config.get("image_max_items", 8))))
        except Exception:
            return 8

    def _image_font(self) -> str:
        return str(self.config.get("image_font", "") or "").strip()

    def _cleanup_images(self, keep_hours: int = 24, keep_count: int = 200):
        """清理历史图片：超过 keep_hours 的直接删，再按数量只留最新 keep_count 张。

        图片是每次出图新生成的临时文件（发完就没用了），这里兜住磁盘不被撑爆。
        """
        image_dir = self._image_dir()
        try:
            if not os.path.isdir(image_dir):
                return
            cutoff = datetime.now().timestamp() - keep_hours * 3600
            remain = []
            for name in os.listdir(image_dir):
                if not name.endswith(".png"):
                    continue
                path = os.path.join(image_dir, name)
                try:
                    mtime = os.path.getmtime(path)
                except Exception:
                    continue
                if mtime < cutoff:
                    try:
                        os.remove(path)
                    except Exception:
                        pass
                    continue
                remain.append((mtime, path))
            # 数量兜底：短时间被刷很多张时，只留最新的 keep_count 张
            if len(remain) > keep_count:
                remain.sort()  # 旧 → 新
                for _mtime, path in remain[: len(remain) - keep_count]:
                    try:
                        os.remove(path)
                    except Exception:
                        pass
        except Exception:
            pass

    def _maybe_cleanup_images(self):
        """出图后顺手清理，节流 6 小时一次（避免只靠插件重载才清）。"""
        now = datetime.now().timestamp()
        if now - getattr(self, "_last_cleanup", 0.0) < 6 * 3600:
            return
        self._last_cleanup = now
        self._cleanup_images()

    def _image_chain(self, event, path: str):
        """构造图片消息链；构造不出来返回 None。"""
        try:
            import astrbot.api.message_components as comps

            return event.chain_result([comps.Image.fromFileSystem(path)])
        except Exception:
            pass
        image_result = getattr(event, "image_result", None)
        if callable(image_result):
            try:
                return image_result(path)
            except Exception:
                pass
        return None

    async def _reply_image(self, event, path: str, fallback: str):
        """发图片，失败自动降级为纯文本。"""
        chain = self._image_chain(event, path)
        if chain is None:
            yield event.plain_result(fallback)
            return
        try:
            await event.send(chain)
        except Exception as exc:
            logger.warning(f"随手记: 图片发送失败（{exc}），降级纯文本")
            yield event.plain_result(fallback)

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
            if memostore.is_overdue(n, now):
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

    # ------------------------------------------------------------ 图片版清单 / 周视图

    def _row(self, n: dict) -> dict:
        return {
            "id": n["id"], "text": n["text"], "priority": n.get("priority", 1),
            "_due": memostore._due_of(n), "has_time": bool(n.get("has_time")),
        }

    def _list_buckets(self, key: str):
        """清单图片数据：(buckets, stats)。

        buckets: [{name,color,items}]；stats: (total, overdue, today, week)
        """
        now = datetime.now()
        data = self._load()
        group = self._group(data, key)
        notes = memostore.sort_notes(
            [n for n in group["notes"] if not n.get("done")], now
        )
        overdue: list[dict] = []
        urgent: list[dict] = []
        timed: list[dict] = []
        floating: list[dict] = []
        today_count = 0
        week_count = 0
        _, week_end = memostore.week_of(now, 0)
        for n in notes:
            due = memostore._due_of(n)
            if due:
                if due.date() == now.date():
                    today_count += 1
                if due < week_end:
                    week_count += 1
            if memostore.is_overdue(n, now):
                overdue.append(n)
            elif n.get("priority") == 2:
                urgent.append(n)
            elif due:
                timed.append(n)
            else:
                floating.append(n)
        buckets = [
            {"name": "已过期", "color": "#D6453B", "items": [self._row(n) for n in overdue]},
            {"name": "紧急 / 重要", "color": "#D6453B", "items": [self._row(n) for n in urgent]},
            {"name": "有时间", "color": "#3F7EBD", "items": [self._row(n) for n in timed]},
            {"name": "随手记", "color": "#A8A29A", "items": [self._row(n) for n in floating]},
        ]
        return buckets, (len(notes), len(overdue), today_count, week_count)

    def _week_plan(self, key: str, offset: int = 0):
        """周视图图片数据：(plan, week_start)。"""
        now = datetime.now()
        data = self._load()
        group = self._group(data, key)
        notes = [n for n in group["notes"] if not n.get("done")]
        week_start, week_end = memostore.week_of(now, offset)
        overdue, days, floating = memostore.group_by_day(notes, week_start, now)
        today = now.date()
        plan_days = []
        count_in_week = 0
        for i, day_notes in enumerate(days):
            d = week_start.date() + timedelta(days=i)
            plan_days.append({
                "label": memostore._WEEK_CN[i],
                "date": f"{d.month:02d}-{d.day:02d}",
                "today": d == today,
                "items": [self._row(n) for n in day_notes],
            })
            count_in_week += len(day_notes)
        plan = {
            "days": plan_days,
            "overdue": [self._row(n) for n in overdue],
            "later": [self._row(n) for n in floating if memostore._due_of(n)],
            "floating": [self._row(n) for n in floating if not memostore._due_of(n)],
        }
        return plan, week_start, week_end, count_in_week + len(plan["overdue"])

    def _render_list(self, key: str):
        """出清单图，返回 (图片路径, 降级文本)。渲染不可用/空清单返回 (None, 文本)。"""
        fallback = self._build_list(key)
        if not self._image_enabled():
            return None, fallback
        out = ""
        try:
            buckets, stats = self._list_buckets(key)
            total, overdue_n, today_n, week_n = stats
            if total == 0:
                return None, fallback
            now = datetime.now()
            out = os.path.join(
                self._image_dir(),
                f"list_{now.strftime('%Y%m%d%H%M%S')}_{os.urandom(3).hex()}.png",
            )
            path = render.render_list_image(
                buckets, now, out,
                title=f"清单 · {total} 条未完成",
                subtitle=f"已过期 {overdue_n} · 今天 {today_n} · 本周 {week_n}",
                right=f"{now.month:02d}-{now.day:02d} {memostore._WEEK_CN[now.weekday()]}",
                footer="随手记 · 「清单 完成 编号」打勾 · 「清单 图」再来一张",
                max_items=self._image_max_items(),
                font_path=self._image_font(),
            )
            self._maybe_cleanup_images()
            return path, fallback
        except Exception as exc:
            logger.warning(f"随手记: 清单图渲染失败（{exc}），降级纯文本")
            self._remove_quietly(out)
            return None, fallback

    def _render_week(self, key: str, offset: int = 0):
        """出周视图图片（默认课表网格样式），返回 (图片路径, 降级文本)。"""
        fallback = self._build_week(key, offset)
        if not self._image_enabled():
            return None, fallback
        out = ""
        try:
            plan, week_start, week_end, count = self._week_plan(key, offset)
            now = datetime.now()
            if count == 0 and not plan["later"] and not plan["floating"]:
                return None, fallback
            title = "本周课表" if offset == 0 else ("下周课表" if offset == 1 else f"+{offset} 周课表")
            if not self.config.get("image_week_timetable", True):
                title = title.replace("课表", "安排")
            end_d = week_end - timedelta(days=1)
            out = os.path.join(
                self._image_dir(),
                f"week_{now.strftime('%Y%m%d%H%M%S')}_{os.urandom(3).hex()}.png",
            )
            common = dict(
                title=title,
                subtitle=f"{week_start.month:02d}-{week_start.day:02d} ~ "
                         f"{end_d.month:02d}-{end_d.day:02d} · 共 {count} 条",
                right=f"第 {now.isocalendar()[1] + offset} 周",
                footer="随手记 · 说「记 周三晚8点开会」就能排进来",
                font_path=self._image_font(),
            )
            if self.config.get("image_week_timetable", True):
                path = render.render_timetable_image(plan, now, out, **common)
            else:
                path = render.render_week_image(plan, now, out, per_day=6, **common)
            self._maybe_cleanup_images()
            return path, fallback
        except Exception as exc:
            logger.warning(f"随手记: 周视图图渲染失败（{exc}），降级纯文本")
            self._remove_quietly(out)
            return None, fallback

    @staticmethod
    def _remove_quietly(path: str):
        """删掉半截/残留的图片文件，失败不管。"""
        if not path:
            return
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass

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

    # ------------------------------------------------------------ WebUI 面板 API

    def _register_web_apis(self):
        """把可视化面板要用的接口注册给 Dashboard。

        视图里通过 bridge.apiGet("overview") 调用，Dashboard 会转发到
        /api/v1/plugins/extensions/<插件名>/overview。
        """
        register_api = getattr(getattr(self, "context", None), "register_web_api", None)
        if not _WEB_AVAILABLE or not callable(register_api):
            logger.info("随手记: 当前 AstrBot 不支持插件 Web API，跳过可视化面板注册")
            return
        routes = (
            ("overview", self.api_overview, ["GET"], "随手记：概览统计"),
            ("notes", self.api_notes, ["GET"], "随手记：清单数据"),
            ("notes/add", self.api_add_note, ["POST"], "随手记：新增一条"),
            ("notes/update", self.api_update_note, ["POST"], "随手记：编辑一条"),
            ("notes/complete", self.api_complete_note, ["POST"], "随手记：完成/恢复"),
            ("notes/delete", self.api_delete_note, ["POST"], "随手记：删除一条"),
            ("week", self.api_week, ["GET"], "随手记：周视图"),
            ("config", self.api_get_config, ["GET"], "随手记：读取配置"),
            ("config/save", self.api_save_config, ["POST"], "随手记：保存配置"),
        )
        for path, handler, methods, desc in routes:
            try:
                register_api(f"/{_PLUGIN_ID}/{path}", handler, methods, desc)
            except Exception as exc:
                logger.warning(f"随手记: 注册面板接口 {path} 失败: {exc}")

    @staticmethod
    def _session_label(key: str) -> str:
        """把 aiocqhttp:GroupMessage:123 变成「群 · 123」。"""
        parts = [p for p in str(key or "").split(":") if p]
        if len(parts) >= 3:
            kind = {
                "GroupMessage": "群聊",
                "FriendMessage": "私聊",
                "GuildMessage": "频道",
                "TempMessage": "临时会话",
            }.get(parts[1], parts[1])
            return f"{kind} · {parts[-1]}"
        return str(key or "未命名会话")

    def _peek_group(self, data: dict, key: str) -> dict:
        """只读地取会话分组（不存在时返回空结构，不写盘）。"""
        raw = (data.get("groups") or {}).get(key)
        return memostore.ensure_group_shape(raw) if isinstance(raw, dict) else memostore.new_group()

    @staticmethod
    def _as_bool(value, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on", "是", "开")
        return default

    @staticmethod
    def _bucket_of(note: dict, now: datetime) -> str:
        due = memostore._due_of(note)
        if due and due < now:
            return "overdue"
        if note.get("priority") == 2:
            return "urgent"
        if due:
            return "timed"
        return "floating"

    def _note_payload(self, note: dict, now: datetime) -> dict:
        due = memostore._due_of(note)
        priority = note.get("priority")
        return {
            "id": note.get("id"),
            "text": note.get("text", ""),
            "due": note.get("due"),
            "has_time": bool(note.get("has_time")),
            "priority": priority if priority in (0, 1, 2) else 1,
            "done": bool(note.get("done")),
            "created_at": note.get("created_at", ""),
            "created_by": note.get("created_by", ""),
            "due_text": memostore.fmt_due(note, now),
            "overdue": bool(due and due < now),
            "bucket": self._bucket_of(note, now),
            "weekday": memostore._WEEK_CN[due.weekday()] if due else "",
            "date": due.strftime("%Y-%m-%d") if due else "",
            "clock": due.strftime("%H:%M") if due else "",
        }

    def _config_payload(self) -> dict:
        try:
            max_display = int(self.config.get("max_display", 10))
        except (TypeError, ValueError):
            max_display = 10
        return {
            "auto_capture": self._as_bool(self.config.get("auto_capture", True), True),
            "llm_tools": self._as_bool(self.config.get("llm_tools", True), True),
            "max_display": max_display,
        }

    def _persist_config(self) -> bool:
        saver = getattr(self.config, "save_config", None)
        if not callable(saver):
            return False
        try:
            saver()
            return True
        except Exception as exc:
            logger.error(f"随手记: 保存配置失败: {exc}")
            return False

    async def api_overview(self):
        """所有会话的统计：未完成/已过期/今天/本周/已完成。"""
        now = datetime.now()
        data = self._load()
        week_start, week_end = memostore.week_of(now, 0)
        today = now.date()
        sessions: list[dict] = []
        totals = {"open": 0, "done": 0, "overdue": 0, "today": 0, "week": 0, "sessions": 0}
        for key, raw in (data.get("groups") or {}).items():
            group = memostore.ensure_group_shape(raw) if isinstance(raw, dict) else memostore.new_group()
            notes = [n for n in group.get("notes", []) if not n.get("done")]
            done = group.get("done") or []
            if not notes and not done:
                continue
            overdue = today_n = week_n = 0
            for note in notes:
                due = memostore._due_of(note)
                if not due:
                    continue
                if due < now:
                    overdue += 1
                if due.date() == today:
                    today_n += 1
                if week_start <= due < week_end:
                    week_n += 1
            sessions.append({
                "key": key,
                "label": self._session_label(key),
                "open": len(notes),
                "done": len(done),
                "overdue": overdue,
                "today": today_n,
                "week": week_n,
            })
            for field, value in (("open", len(notes)), ("done", len(done)),
                                 ("overdue", overdue), ("today", today_n), ("week", week_n)):
                totals[field] += value
        sessions.sort(key=lambda s: (-s["open"], s["key"]))
        totals["sessions"] = len(sessions)
        return json_response({
            "sessions": sessions,
            "totals": totals,
            "now": now.strftime("%Y-%m-%d %H:%M"),
            "week_range": [week_start.strftime("%m-%d"), (week_end - timedelta(days=1)).strftime("%m-%d")],
        })

    async def api_notes(self):
        """某个会话的清单（默认未完成，按 过期>紧急>有时间>随手记 排序）。"""
        key = (web_request.query.get("session") or "").strip()
        status = (web_request.query.get("status") or "open").strip().lower()
        keyword = (web_request.query.get("q") or "").strip()
        if not key:
            return error_response("缺少 session 参数")
        now = datetime.now()
        group = self._peek_group(self._load(), key)
        if status == "done":
            notes = list(reversed(group.get("done") or []))
        else:
            notes = memostore.sort_notes(
                [n for n in group.get("notes", []) if not n.get("done")], now
            )
        if keyword:
            kw = keyword.lower()
            notes = [n for n in notes if kw in str(n.get("text", "")).lower()]
        return json_response({
            "session": key,
            "label": self._session_label(key),
            "status": status,
            "notes": [self._note_payload(n, now) for n in notes],
            "counts": {
                "open": len([n for n in group.get("notes", []) if not n.get("done")]),
                "done": len(group.get("done") or []),
            },
        })

    async def api_week(self):
        """周视图：offset=0 本周，1 下周，-1 上周。"""
        key = (web_request.query.get("session") or "").strip()
        if not key:
            return error_response("缺少 session 参数")
        try:
            offset = int(web_request.query.get("offset", 0, type=int) or 0)
        except (TypeError, ValueError):
            offset = 0
        offset = max(-8, min(8, offset))
        now = datetime.now()
        week_start, week_end = memostore.week_of(now, offset)
        group = self._peek_group(self._load(), key)
        notes = [n for n in group.get("notes", []) if not n.get("done")]
        overdue, days, floating = memostore.group_by_day(notes, week_start, now)
        day_list = []
        for i, items in enumerate(days):
            day = week_start.date() + timedelta(days=i)
            day_list.append({
                "date": day.strftime("%Y-%m-%d"),
                "label": memostore._WEEK_CN[i],
                "short": f"{day.month:02d}-{day.day:02d}",
                "is_today": day == now.date(),
                "notes": [self._note_payload(n, now) for n in items],
            })
        beyond = [n for n in floating if memostore._due_of(n)]
        pure = [n for n in floating if not memostore._due_of(n)]
        return json_response({
            "session": key,
            "offset": offset,
            "title": "本周" if offset == 0 else ("下周" if offset == 1 else ("上周" if offset == -1 else f"{offset:+d} 周")),
            "start": week_start.strftime("%Y-%m-%d"),
            "end": (week_end - timedelta(days=1)).strftime("%Y-%m-%d"),
            "today": now.strftime("%Y-%m-%d"),
            "days": day_list,
            "overdue": [self._note_payload(n, now) for n in overdue],
            "beyond": [self._note_payload(n, now) for n in beyond],
            "floating": [self._note_payload(n, now) for n in pure],
        })

    async def api_add_note(self):
        """面板新增：content 里可以带中文时间，也可以直接给 due（YYYY-MM-DDTHH:MM）。"""
        payload = await web_request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体应为 JSON 对象")
        key = str(payload.get("session") or "").strip()
        content = str(payload.get("content") or "").strip()
        if not key:
            return error_response("缺少 session 参数")
        if not content:
            return error_response("内容不能为空")
        if len(content) > 200:
            return error_response("内容太长了（最多 200 字）")

        try:
            priority_hint = int(payload.get("priority"))
        except (TypeError, ValueError):
            priority_hint = None
        if priority_hint not in (0, 1, 2):
            priority_hint = None

        text = content
        due, has_time = None, False
        from_text_priority = 1
        raw_due = str(payload.get("due") or "").strip()
        if raw_due:
            try:
                due = datetime.strptime(raw_due.replace("T", " ")[:16], "%Y-%m-%d %H:%M")
            except ValueError:
                return error_response("时间格式应为 2026-11-12T15:00")
            has_time = True
        else:
            info = parse.parse_note(content)
            if not info:
                return error_response("没解析出要记的内容")
            text, due, has_time = info["text"], info["due"], info["has_time"]
            from_text_priority = info["priority"]

        data = self._load()
        group = self._group(data, key)
        try:
            note = memostore.add_note(
                group, text, due=due, has_time=has_time,
                priority=priority_hint if priority_hint is not None else from_text_priority,
                uid=f"web:{web_request.username or 'dashboard'}",
            )
        except MemoError as exc:
            return error_response(str(exc))
        self._save(data)
        return json_response({"ok": True, "session": key, "note": self._note_payload(note, datetime.now())})

    async def api_update_note(self):
        """面板编辑：text / priority / due（传空字符串表示清掉时间）。"""
        payload = await web_request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体应为 JSON 对象")
        key = str(payload.get("session") or "").strip()
        try:
            nid = int(payload.get("id"))
        except (TypeError, ValueError):
            return error_response("缺少有效的编号")
        if not key:
            return error_response("缺少 session 参数")
        data = self._load()
        group = self._group(data, key)
        note = memostore.get_note(group, nid)
        if not note:
            return error_response(f"没有 #{nid} 这条随记", 404)

        if "text" in payload:
            text = " ".join(str(payload.get("text") or "").split())[:80]
            if not text:
                return error_response("内容不能为空")
            note["text"] = text
        if "priority" in payload:
            try:
                priority = int(payload.get("priority"))
            except (TypeError, ValueError):
                return error_response("优先级应为 0/1/2")
            if priority not in (0, 1, 2):
                return error_response("优先级应为 0/1/2")
            note["priority"] = priority
        if "due" in payload:
            raw = str(payload.get("due") or "").strip()
            if not raw:
                note["due"] = None
                note["has_time"] = False
            else:
                try:
                    due = datetime.strptime(raw.replace("T", " ")[:16], "%Y-%m-%d %H:%M")
                except ValueError:
                    return error_response("时间格式应为 2026-11-12T15:00")
                note["due"] = due.strftime("%Y-%m-%d %H:%M")
                note["has_time"] = True
        self._save(data)
        return json_response({"ok": True, "session": key, "note": self._note_payload(note, datetime.now())})

    async def api_complete_note(self):
        """完成 / 取消完成（undo=true 从已完成恢复）。"""
        payload = await web_request.json(default={})
        key = str((payload or {}).get("session") or "").strip()
        try:
            nid = int((payload or {}).get("id"))
        except (TypeError, ValueError):
            return error_response("缺少有效的编号")
        undo = self._as_bool((payload or {}).get("undo"), False)
        if not key:
            return error_response("缺少 session 参数")
        data = self._load()
        group = self._group(data, key)
        if undo:
            done_list = group.get("done") or []
            target = next((n for n in done_list if n.get("id") == nid), None)
            if not target:
                return error_response(f"已完成记录里没有 #{nid}", 404)
            done_list.remove(target)
            target["done"] = False
            target.pop("done_at", None)
            group.setdefault("notes", []).append(target)
        else:
            try:
                memostore.complete_note(group, nid)
            except MemoError as exc:
                return error_response(str(exc), 404)
        self._save(data)
        return json_response({"ok": True, "id": nid, "undo": undo})

    async def api_delete_note(self):
        payload = await web_request.json(default={})
        key = str((payload or {}).get("session") or "").strip()
        try:
            nid = int((payload or {}).get("id"))
        except (TypeError, ValueError):
            return error_response("缺少有效的编号")
        if not key:
            return error_response("缺少 session 参数")
        data = self._load()
        group = self._group(data, key)
        done_list = group.get("done") or []
        target = next((n for n in done_list if n.get("id") == nid and n.get("done")), None)
        if target:
            done_list.remove(target)  # 已完成里的历史记录直接抹掉
        else:
            try:
                memostore.remove_note(group, nid)
            except MemoError as exc:
                return error_response(str(exc), 404)
        self._save(data)
        return json_response({"ok": True, "id": nid})

    async def api_get_config(self):
        return json_response({
            "config": self._config_payload(),
            "persisted": callable(getattr(self.config, "save_config", None)),
        })

    async def api_save_config(self):
        payload = await web_request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体应为 JSON 对象")
        updates: dict = {}
        if "auto_capture" in payload:
            if not isinstance(payload["auto_capture"], bool):
                return error_response("auto_capture 应为布尔值")
            updates["auto_capture"] = payload["auto_capture"]
        if "llm_tools" in payload:
            if not isinstance(payload["llm_tools"], bool):
                return error_response("llm_tools 应为布尔值")
            updates["llm_tools"] = payload["llm_tools"]
        if "max_display" in payload:
            try:
                max_display = int(payload["max_display"])
            except (TypeError, ValueError):
                return error_response("max_display 应为整数")
            if not 3 <= max_display <= 50:
                return error_response("max_display 应在 3~50 之间")
            updates["max_display"] = max_display
        if not updates:
            return error_response("没有可更新的配置项")
        for field, value in updates.items():
            self.config[field] = value
        persisted = self._persist_config()
        if not persisted:
            logger.warning("随手记: 配置已更新到内存，但当前环境无法写回配置文件")
        return json_response({"ok": True, "config": self._config_payload(), "persisted": persisted})

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
        """清单管理：清单 / 清单 图 / 清单 本周 / 清单 完成 3 / 清单 删 3"""
        key = event.unified_msg_origin
        raw = _strip_cmd(event.message_str, "清单", "随记", "备忘", "待办", "todo", "我的清单").strip()
        word = raw.split(maxsplit=1)[0] if raw else ""
        rest = raw[len(word):].strip() if raw else ""

        if word in ("图", "图片", "看图"):
            path, fallback = self._render_list(key)
            if path:
                async for r in self._reply_image(event, path, fallback):
                    yield r
            else:
                yield event.plain_result(fallback)
            return
        if word in ("本周", "这周", "这礼拜", "本周安排"):
            if rest.endswith("图"):
                path, fallback = self._render_week(key, 0)
                if path:
                    async for r in self._reply_image(event, path, fallback):
                        yield r
                else:
                    yield event.plain_result(fallback)
            else:
                yield event.plain_result(self._build_week(key, 0))
            return
        if word in ("下周", "下礼拜"):
            if rest.endswith("图"):
                path, fallback = self._render_week(key, 1)
                if path:
                    async for r in self._reply_image(event, path, fallback):
                        yield r
                else:
                    yield event.plain_result(fallback)
            else:
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
        """本周安排：本周 / 本周 图 / 本周 下周图"""
        arg = _strip_cmd(event.message_str, "本周", "周视图", "周安排", "week").strip()
        want_img = "图" in arg
        arg = arg.replace("图", "").replace("片", "").strip()
        offset = 1 if arg in ("下周", "下礼拜", "next") else 0
        if want_img:
            path, fallback = self._render_week(event.unified_msg_origin, offset)
            if path:
                async for r in self._reply_image(event, path, fallback):
                    yield r
            else:
                yield event.plain_result(fallback)
        else:
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
    async def tool_list(self, event: AstrMessageEvent, as_image: bool = False):
        """列出当前会话的随手记清单（按 已过期/紧急/有时间/随手记 分组）。用户问「我最近都有啥事」「记了什么」「有哪些待办」时调用。用户想看图片版时（如「发张图看看」「图上给我看」）把 as_image 设为 true。

        Args:
            as_image(boolean): 用户想要图片形式时填 true，默认 false
        """
        if not self._llm_enabled():
            yield event.plain_result("AI 查询已在插件配置里关闭，改用「清单」命令吧。")
            return
        try:
            if as_image:
                path, fallback = self._render_list(event.unified_msg_origin)
                if path:
                    async for r in self._reply_image(event, path, fallback):
                        yield r
                else:
                    yield event.plain_result(fallback)
                return
            text = self._build_list(event.unified_msg_origin)
        except Exception as exc:
            logger.error(f"随手记: AI 查清单失败: {exc}")
            text = "查询出错了，改用「清单」命令试试。"
        yield event.plain_result(text)

    @_llm_tool(name="quicknote_week")
    async def tool_week(self, event: AstrMessageEvent, week_offset: float = 0, as_image: bool = False):
        """按时间排成本周（或下周）的周视图。用户问「这周有什么安排」「下周要干嘛」「周三有什么事」时调用。用户想看图片日历时把 as_image 设为 true。

        Args:
            week_offset(number): 0 表示本周（默认），1 表示下周
            as_image(boolean): 用户想要图片日历形式时填 true，默认 false
        """
        if not self._llm_enabled():
            yield event.plain_result("AI 查询已在插件配置里关闭，改用「清单 本周」命令吧。")
            return
        try:
            offset = 1 if int(week_offset or 0) >= 1 else 0
            if as_image:
                path, fallback = self._render_week(event.unified_msg_origin, offset)
                if path:
                    async for r in self._reply_image(event, path, fallback):
                        yield r
                else:
                    yield event.plain_result(fallback)
                return
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
