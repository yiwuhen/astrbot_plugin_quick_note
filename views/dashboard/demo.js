/**
 * 预览模式用的假后端：只有页面脱离 AstrBot WebUI 单独打开时才启用，
 * 方便调样式、截屏看效果。接入 Dashboard 后不会走这里。
 */

export function createDemoApi() {
  const pad = (n) => String(n).padStart(2, "0");
  const fmtDate = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const fmt = (d) => `${fmtDate(d)} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  const at = (days, h, m) => {
    const d = new Date();
    d.setDate(d.getDate() + days);
    d.setHours(h, m, 0, 0);
    return fmt(d);
  };
  const parse = (s) => {
    const [ds, ts] = String(s).split(" ");
    const [y, mo, da] = ds.split("-").map(Number);
    const [h, mi] = (ts || "09:00").split(":").map(Number);
    return new Date(y, mo - 1, da, h, mi);
  };
  const WEEK_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];

  const state = {
    auto_capture: true,
    llm_tools: true,
    max_display: 10,
    nextId: 100,
    groups: {
      "aiocqhttp:GroupMessage:778899": {
        label: "群聊 · 778899",
        notes: [
          { id: 7, text: "高数作业第三章（P82 全部）", due: at(1, 23, 0), has_time: true, priority: 2 },
          { id: 6, text: "交周报给组长", due: at(3, 17, 0), has_time: true, priority: 1 },
          { id: 5, text: "取快递（菜鸟驿站）", due: at(0, 18, 30), has_time: true, priority: 1 },
          { id: 4, text: "买牛奶", due: null, has_time: false, priority: 0 },
          { id: 3, text: "预约体检", due: at(5, 9, 0), has_time: true, priority: 1 },
          { id: 2, text: "整理房间", due: null, has_time: false, priority: 1 },
          { id: 8, text: "交实验报告", due: at(-1, 20, 0), has_time: true, priority: 1 },
        ],
        done: [
          { id: 1, text: "洗车", due: at(-2, 10, 0), has_time: true, priority: 1, done: true, done_at: at(-1, 9, 0) },
        ],
      },
      "aiocqhttp:FriendMessage:10001": {
        label: "私聊 · 10001",
        notes: [{ id: 1, text: "买猫粮", due: null, has_time: false, priority: 1 }],
        done: [],
      },
    },
  };

  const groupOf = (key) =>
    state.groups[key] || (state.groups[key] = { label: key, notes: [], done: [] });

  function fmtDue(note, now) {
    if (!note.due) return "";
    const due = parse(note.due);
    let base = `${pad(due.getMonth() + 1)}-${pad(due.getDate())}`;
    if (note.has_time) base += ` ${pad(due.getHours())}:${pad(due.getMinutes())}`;
    if (due < now) {
      const hours = Math.floor((now - due) / 3600000);
      if (hours >= 48) base += `（已过 ${Math.floor(hours / 24)} 天）`;
      else if (hours >= 1) base += `（已过 ${hours} 小时）`;
      else base = "（刚刚到期）" + base;
    }
    return base;
  }

  function payload(note, now) {
    const due = note.due ? parse(note.due) : null;
    const overdue = !!due && due < now;
    return {
      id: note.id,
      text: note.text,
      due: note.due,
      has_time: !!note.has_time,
      priority: note.priority ?? 1,
      done: !!note.done,
      created_at: note.created_at || "",
      created_by: note.created_by || "demo",
      due_text: fmtDue(note, now),
      overdue,
      bucket: overdue ? "overdue" : note.priority === 2 ? "urgent" : due ? "timed" : "floating",
      weekday: due ? WEEK_CN[(due.getDay() + 6) % 7] : "",
      date: due ? fmtDate(due) : "",
      clock: due ? `${pad(due.getHours())}:${pad(due.getMinutes())}` : "",
    };
  }

  function sortNotes(notes, now) {
    return notes.slice().sort((a, b) => {
      const da = a.due ? parse(a.due) : null;
      const db = b.due ? parse(b.due) : null;
      const ba = da && da < now ? 0 : da ? 1 : 2;
      const bb = db && db < now ? 0 : db ? 1 : 2;
      if (ba !== bb) return ba - bb;
      if ((b.priority ?? 1) !== (a.priority ?? 1)) return (b.priority ?? 1) - (a.priority ?? 1);
      if (da && db) return da - db;
      return da ? 1 : db ? -1 : 0;
    });
  }

  function mondayOf(offset) {
    const now = new Date();
    const d = new Date(now);
    d.setHours(0, 0, 0, 0);
    d.setDate(d.getDate() - ((now.getDay() + 6) % 7) + offset * 7);
    return d;
  }

  return {
    get(endpoint, params) {
      const now = new Date();
      if (endpoint === "overview") {
        const sessions = [];
        const totals = { open: 0, done: 0, overdue: 0, today: 0, week: 0, sessions: 0 };
        const monday = mondayOf(0);
        const sunday = new Date(monday);
        sunday.setDate(sunday.getDate() + 7);
        Object.entries(state.groups).forEach(([key, g]) => {
          const notes = g.notes.filter((n) => !n.done);
          const overdue = notes.filter((n) => n.due && parse(n.due) < now).length;
          const today = notes.filter((n) => n.due && fmtDate(parse(n.due)) === fmtDate(now)).length;
          const week = notes.filter((n) => n.due && parse(n.due) >= monday && parse(n.due) < sunday).length;
          sessions.push({
            key,
            label: g.label,
            open: notes.length,
            done: g.done.length,
            overdue,
            today,
            week,
          });
          totals.open += notes.length;
          totals.done += g.done.length;
          totals.overdue += overdue;
          totals.today += today;
          totals.week += week;
        });
        sessions.sort((a, b) => b.open - a.open);
        totals.sessions = sessions.length;
        return {
          sessions,
          totals,
          now: fmt(now),
          week_range: [fmtDate(monday).slice(5), fmtDate(new Date(sunday - 86400000)).slice(5)],
        };
      }

      if (endpoint === "notes") {
        const g = groupOf(params.session);
        const status = params.status || "open";
        let notes =
          status === "done"
            ? g.done.slice().reverse()
            : sortNotes(g.notes.filter((n) => !n.done), now);
        const q = (params.q || "").toLowerCase();
        if (q) notes = notes.filter((n) => n.text.toLowerCase().includes(q));
        return {
          session: params.session,
          label: g.label,
          status,
          notes: notes.map((n) => payload(n, now)),
          counts: { open: g.notes.filter((n) => !n.done).length, done: g.done.length },
        };
      }

      if (endpoint === "week") {
        const g = groupOf(params.session);
        const offset = Number(params.offset || 0);
        const monday = mondayOf(offset);
        const end = new Date(monday);
        end.setDate(end.getDate() + 7);
        const notes = g.notes.filter((n) => !n.done);
        const days = [];
        for (let i = 0; i < 7; i += 1) {
          const day = new Date(monday);
          day.setDate(day.getDate() + i);
          const items = notes
            .filter((n) => n.due && fmtDate(parse(n.due)) === fmtDate(day))
            .map((n) => payload(n, now))
            .sort((a, b) => (a.clock || "").localeCompare(b.clock || ""));
          days.push({
            date: fmtDate(day),
            label: WEEK_CN[i],
            short: fmtDate(day).slice(5),
            is_today: fmtDate(day) === fmtDate(now),
            notes: items,
          });
        }
        return {
          session: params.session,
          offset,
          title: offset === 0 ? "本周" : offset === 1 ? "下周" : offset === -1 ? "上周" : `${offset > 0 ? "+" : ""}${offset} 周`,
          start: fmtDate(monday),
          end: fmtDate(new Date(end - 86400000)),
          today: fmtDate(now),
          days,
          overdue: notes.filter((n) => n.due && parse(n.due) < monday).map((n) => payload(n, now)),
          beyond: notes.filter((n) => n.due && parse(n.due) >= end).map((n) => payload(n, now)),
          floating: notes.filter((n) => !n.due).map((n) => payload(n, now)),
        };
      }

      if (endpoint === "config") {
        return {
          config: {
            auto_capture: state.auto_capture,
            llm_tools: state.llm_tools,
            max_display: state.max_display,
          },
          persisted: true,
        };
      }

      throw new Error(`未知接口：${endpoint}`);
    },

    post(endpoint, body) {
      const now = new Date();
      if (endpoint === "notes/add") {
        const g = groupOf(body.session);
        const note = {
          id: state.nextId++,
          text: String(body.content || "").trim(),
          due: body.due ? String(body.due).replace("T", " ") : null,
          has_time: !!body.due,
          priority: Number(body.priority ?? 1),
          done: false,
        };
        if (!note.text) throw new Error("内容不能为空");
        g.notes.push(note);
        return { ok: true, session: body.session, note: payload(note, now) };
      }

      if (endpoint === "notes/update") {
        const g = groupOf(body.session);
        const note = g.notes.find((n) => n.id === Number(body.id));
        if (!note) throw new Error(`没有 #${body.id} 这条随记`);
        if (body.text !== undefined) note.text = String(body.text).trim();
        if (body.priority !== undefined) note.priority = Number(body.priority);
        if (body.due !== undefined) {
          note.due = body.due ? String(body.due).replace("T", " ") : null;
          note.has_time = !!body.due;
        }
        return { ok: true, session: body.session, note: payload(note, now) };
      }

      if (endpoint === "notes/complete") {
        const g = groupOf(body.session);
        const id = Number(body.id);
        if (body.undo) {
          const idx = g.done.findIndex((n) => n.id === id);
          if (idx < 0) throw new Error(`已完成记录里没有 #${id}`);
          const [note] = g.done.splice(idx, 1);
          note.done = false;
          delete note.done_at;
          g.notes.push(note);
        } else {
          const idx = g.notes.findIndex((n) => n.id === id);
          if (idx < 0) throw new Error(`没有 #${id} 这条随记`);
          const [note] = g.notes.splice(idx, 1);
          note.done = true;
          note.done_at = fmt(now);
          g.done.push(note);
        }
        return { ok: true, id };
      }

      if (endpoint === "notes/delete") {
        const g = groupOf(body.session);
        const id = Number(body.id);
        const di = g.done.findIndex((n) => n.id === id);
        if (di >= 0) g.done.splice(di, 1);
        else {
          const ni = g.notes.findIndex((n) => n.id === id);
          if (ni < 0) throw new Error(`没有 #${id} 这条随记`);
          g.notes.splice(ni, 1);
        }
        return { ok: true, id };
      }

      if (endpoint === "config/save") {
        if (typeof body.auto_capture === "boolean") state.auto_capture = body.auto_capture;
        if (typeof body.llm_tools === "boolean") state.llm_tools = body.llm_tools;
        if (body.max_display !== undefined) state.max_display = Number(body.max_display);
        return {
          ok: true,
          config: {
            auto_capture: state.auto_capture,
            llm_tools: state.llm_tools,
            max_display: state.max_display,
          },
          persisted: true,
        };
      }

      throw new Error(`未知接口：${endpoint}`);
    },
  };
}
