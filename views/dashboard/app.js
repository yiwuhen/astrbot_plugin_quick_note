/* 随手记 · WebUI 数据面板
 * 通过 window.AstrBotPluginView bridge 调用插件后端接口。
 * 单独打开（没有 bridge）时走 demo.js 的假数据，方便调样式。
 */

const bridge = window.AstrBotPluginView || window.AstrBotPluginPage || null;
const DEMO = !bridge;
let demoApi = null;
if (DEMO) {
  const mod = await import("./demo.js");
  demoApi = mod.createDemoApi();
}

const $ = (sel) => document.querySelector(sel);
const esc = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[ch]));

const state = {
  sessions: [],
  session: "",
  tab: "list",
  status: "open",
  q: "",
  notes: [],
  counts: { open: 0, done: 0 },
  weekOffset: 0,
  week: null,
  config: { auto_capture: true, llm_tools: true, max_display: 10 },
  persisted: true,
  editing: null,
  editingDueInit: "",
  priority: 1,
};

let toastTimer = null;
function toast(message, isError = false) {
  const el = $("#toast");
  el.textContent = message;
  el.classList.toggle("error", isError);
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 2600);
}

async function apiGet(endpoint, params) {
  if (DEMO) return demoApi.get(endpoint, params || {});
  try {
    return await bridge.apiGet(endpoint, params || {});
  } catch (err) {
    toast(err?.message || "请求失败", true);
    throw err;
  }
}

async function apiPost(endpoint, body) {
  if (DEMO) return demoApi.post(endpoint, body || {});
  try {
    return await bridge.apiPost(endpoint, body || {});
  } catch (err) {
    toast(err?.message || "请求失败", true);
    throw err;
  }
}

/* ---------------------------------------------------------------- 概览 */

async function refreshOverview() {
  const data = await apiGet("overview");
  state.sessions = data.sessions || [];
  if (!state.session || !state.sessions.some((s) => s.key === state.session)) {
    state.session = state.sessions.length ? state.sessions[0].key : "";
  }
  renderSessions();
  renderStats(data.totals || {}, data.week_range || []);
  const totals = data.totals || {};
  $("#subtitle").textContent = DEMO
    ? "预览模式：数据为示例，操作只在当前页面生效"
    : `${totals.sessions || 0} 个会话 · ${totals.open || 0} 条未完成 · 更新于 ${data.now || ""}`;
}

function renderSessions() {
  const select = $("#session");
  if (!state.sessions.length) {
    select.innerHTML = '<option value="">暂无记录</option>';
    select.disabled = true;
    return;
  }
  select.disabled = false;
  select.innerHTML = state.sessions
    .map((s) => `<option value="${esc(s.key)}">${esc(s.label)}（${s.open}）</option>`)
    .join("");
  select.value = state.session;
  select.title = state.session;
}

function renderStats(totals, weekRange) {
  const cards = [
    { key: "open", label: "未完成", tone: "" },
    { key: "overdue", label: "已过期", tone: "danger" },
    { key: "today", label: "今天到期", tone: "accent" },
    { key: "week", label: "本周", tone: "plain", hint: weekRange.length === 2 ? `${weekRange[0]} ~ ${weekRange[1]}` : "" },
    { key: "done", label: "已完成", tone: "muted" },
  ];
  $("#stats").innerHTML = cards
    .map(
      (card) => `
      <div class="stat ${card.tone}">
        <div class="stat-value">${Number(totals[card.key] || 0)}</div>
        <div class="stat-label">${card.label}</div>
        ${card.hint ? `<div class="stat-hint">${esc(card.hint)}</div>` : ""}
      </div>`
    )
    .join("");
}

/* ---------------------------------------------------------------- 清单 */

async function refreshNotes() {
  if (!state.session) {
    state.notes = [];
    renderList();
    return;
  }
  const params = { session: state.session, status: state.status };
  if (state.q) params.q = state.q;
  const data = await apiGet("notes", params);
  state.notes = data.notes || [];
  state.counts = data.counts || { open: 0, done: 0 };
  renderList();
}

function emptyBlock(title, hint) {
  return `<div class="empty"><div class="empty-icon">🗒</div><h3>${esc(title)}</h3><p>${esc(hint)}</p></div>`;
}

function section(icon, title, count, cls, inner) {
  return `
    <section class="group ${cls}">
      <header class="group-head">
        <span class="group-icon">${icon}</span>
        <span class="group-title">${title}</span>
        <span class="group-count">${count}</span>
      </header>
      <div class="group-body">${inner}</div>
    </section>`;
}

function noteRow(note, done) {
  const chips = [
    `<span class="chip ${note.overdue ? "danger" : ""}">${esc(note.due_text || "不限时间")}</span>`,
  ];
  if (note.priority === 2) chips.push('<span class="chip hot">紧急</span>');
  if (note.priority === 0) chips.push('<span class="chip calm">不急</span>');
  return `
    <article class="note ${done ? "is-done" : ""}" data-id="${note.id}">
      <button class="check" data-act="toggle" title="${done ? "取消完成" : "标记完成"}">${done ? "✓" : ""}</button>
      <div class="note-main">
        <div class="note-text">${esc(note.text)}</div>
        <div class="note-meta">
          ${chips.join("")}
          <span class="note-id">#${note.id}</span>
        </div>
      </div>
      <div class="note-actions">
        ${done ? "" : '<button class="icon" data-act="edit" title="编辑">✏️</button>'}
        <button class="icon" data-act="delete" title="删除">🗑</button>
      </div>
    </article>`;
}

function renderList() {
  const body = $("#list-body");
  if (!state.session) {
    body.innerHTML = emptyBlock("还没有任何会话记录", "在群里发一句「记 明天交报告」，这里就会出现。");
    return;
  }
  if (!state.notes.length) {
    body.innerHTML =
      state.status === "done"
        ? emptyBlock("还没有已完成的记录", "在清单里点左侧圆圈打勾，条目会归档到这里。")
        : emptyBlock("清单是空的，很清爽", "点右上角「＋ 记一条」，或直接在群里说「记住 周五交周报」。");
    return;
  }
  if (state.status === "done") {
    body.innerHTML = section("🗂", "已完成 / 历史", state.notes.length, "muted", state.notes.map((n) => noteRow(n, true)).join(""));
    return;
  }
  const groups = [
    ["overdue", "⚠️", "已过期", "danger"],
    ["urgent", "🔥", "紧急 / 重要", "hot"],
    ["timed", "📅", "有时间", ""],
    ["floating", "📥", "随手记", ""],
  ];
  body.innerHTML = groups
    .map(([key, icon, title, cls]) => {
      const items = state.notes.filter((n) => n.bucket === key);
      if (!items.length) return "";
      return section(icon, title, items.length, cls, items.map((n) => noteRow(n, false)).join(""));
    })
    .join("");
}

async function onListAction(event) {
  const button = event.target.closest("[data-act]");
  if (!button) return;
  const row = button.closest(".note");
  if (!row) return;
  const id = Number(row.dataset.id);
  const note = state.notes.find((n) => n.id === id);
  if (!note) return;
  const act = button.dataset.act;

  if (act === "edit") {
    openModal(note);
    return;
  }
  if (act === "delete") {
    const ok = window.confirm(`删除 #${id}「${note.text}」？\n删除后不可恢复。`);
    if (!ok) return;
    await apiPost("notes/delete", { session: state.session, id });
    toast("已删除");
  }
  if (act === "toggle") {
    const undo = state.status === "done";
    await apiPost("notes/complete", { session: state.session, id, undo });
    toast(undo ? "已恢复到清单" : "已完成 ✓");
  }
  await refreshOverview();
  await refreshNotes();
  if (state.tab === "week") await refreshWeek();
}

/* ---------------------------------------------------------------- 周视图 */

async function refreshWeek() {
  if (!state.session) {
    $("#week-body").innerHTML = emptyBlock("还没有任何会话记录", "先在群里记一条，再来看周视图。");
    $("#week-title").textContent = "本周";
    return;
  }
  const data = await apiGet("week", { session: state.session, offset: state.weekOffset });
  state.week = data;
  renderWeek(data);
}

function weekItem(note) {
  const flag = note.priority === 2 ? "hot" : note.priority === 0 ? "calm" : "";
  return `
    <div class="w-item ${flag}">
      <span class="w-time">${note.has_time ? esc(note.clock) : "全天"}</span>
      <span class="w-text">${esc(note.text)}</span>
    </div>`;
}

function extraCard(icon, title, notes, showDate) {
  return `
    <section class="extra-card">
      <header><span>${icon}</span>${title}<em>${notes.length}</em></header>
      <div class="extra-list">
        ${notes
          .map(
            (n) =>
              `<span class="pill ${n.overdue ? "danger" : ""}">${showDate && n.date ? `<i>${esc(n.date.slice(5))}</i>` : ""}${esc(n.text)}</span>`
          )
          .join("")}
      </div>
    </section>`;
}

function renderWeek(data) {
  $("#week-title").textContent = `${data.title}（${data.start.slice(5)} ~ ${data.end.slice(5)}）`;
  const grid = data.days
    .map(
      (day) => `
      <div class="day ${day.is_today ? "today" : ""}">
        <div class="day-head">
          <span class="day-name">${day.label}</span>
          <span class="day-date">${day.short}</span>
        </div>
        <div class="day-notes">
          ${day.notes.length ? day.notes.map(weekItem).join("") : '<div class="day-empty">·</div>'}
        </div>
      </div>`
    )
    .join("");
  const extras = [];
  if (data.overdue.length) extras.push(extraCard("⚠️", "已过期", data.overdue, true));
  if (data.beyond.length) extras.push(extraCard("🔭", "更远", data.beyond, true));
  if (data.floating.length) extras.push(extraCard("📥", "不限时间", data.floating, false));
  $("#week-body").innerHTML = `<div class="week-grid">${grid}</div>${extras.join("")}`;
}

/* ---------------------------------------------------------------- 配置 */

async function refreshConfig() {
  const data = await apiGet("config");
  state.config = data.config || state.config;
  state.persisted = !!data.persisted;
  $("#cfg-auto").checked = !!state.config.auto_capture;
  $("#cfg-llm").checked = !!state.config.llm_tools;
  $("#cfg-max").value = state.config.max_display ?? 10;
  $("#cfg-hint").textContent = state.persisted
    ? "修改会写回插件配置文件，重载插件后依然生效。"
    : "当前环境无法写回配置文件，修改仅在本次运行生效。";
}

async function saveConfig() {
  const maxDisplay = Number($("#cfg-max").value);
  if (!Number.isInteger(maxDisplay) || maxDisplay < 3 || maxDisplay > 50) {
    toast("每组显示条数应在 3 ~ 50 之间", true);
    return;
  }
  const payload = {
    auto_capture: $("#cfg-auto").checked,
    llm_tools: $("#cfg-llm").checked,
    max_display: maxDisplay,
  };
  const data = await apiPost("config/save", payload);
  state.config = data.config || payload;
  state.persisted = !!data.persisted;
  $("#cfg-hint").textContent = data.persisted
    ? "已保存，聊天里的行为立即生效。"
    : "已在内存生效，但未能写回配置文件。";
  toast(data.persisted ? "配置已保存" : "已生效（未写回文件）");
}

/* ---------------------------------------------------------------- 弹窗 */

function syncPriority() {
  document.querySelectorAll("#f-pri .seg-btn").forEach((btn) => {
    btn.classList.toggle("active", Number(btn.dataset.pri) === state.priority);
  });
}

function openModal(note) {
  state.editing = note || null;
  state.priority = note ? note.priority : 1;
  state.editingDueInit = note && note.has_time && note.due ? note.due.replace(" ", "T") : "";
  $("#modal-title").textContent = note ? `编辑 #${note.id}` : "记一条";
  $("#f-content").value = note ? note.text : "";
  $("#f-due").value = state.editingDueInit;
  $("#modal-err").textContent = "";
  syncPriority();
  $("#modal").classList.remove("hidden");
  setTimeout(() => $("#f-content").focus(), 30);
}

function closeModal() {
  $("#modal").classList.add("hidden");
  state.editing = null;
}

async function submitModal() {
  const content = $("#f-content").value.trim();
  if (!content) {
    $("#modal-err").textContent = "内容不能为空";
    return;
  }
  if (!state.session) {
    $("#modal-err").textContent = "还没有可用的会话，请先在群里记一条";
    return;
  }
  const dueValue = $("#f-due").value;
  try {
    if (state.editing) {
      const payload = { session: state.session, id: state.editing.id, text: content, priority: state.priority };
      if (dueValue !== state.editingDueInit) payload.due = dueValue;
      await apiPost("notes/update", payload);
    } else {
      const payload = { session: state.session, content, priority: state.priority };
      if (dueValue) payload.due = dueValue;
      await apiPost("notes/add", payload);
    }
  } catch (err) {
    $("#modal-err").textContent = err?.message || "保存失败";
    return;
  }
  const wasEditing = !!state.editing;
  closeModal();
  toast(wasEditing ? "已保存修改" : "记下啦");
  await refreshOverview();
  await refreshNotes();
  if (state.tab === "week") await refreshWeek();
}

/* ---------------------------------------------------------------- 事件 */

function switchTab(tab) {
  state.tab = tab;
  document.querySelectorAll("#tabs .tab").forEach((btn) => btn.classList.toggle("active", btn.dataset.tab === tab));
  [["list", "#panel-list"], ["week", "#panel-week"], ["config", "#panel-config"]].forEach(([name, sel]) => {
    $(sel).classList.toggle("hidden", name !== tab);
  });
  if (tab === "week") refreshWeek();
  if (tab === "config") refreshConfig();
}

function bindEvents() {
  $("#tabs").addEventListener("click", (event) => {
    const btn = event.target.closest(".tab");
    if (btn) switchTab(btn.dataset.tab);
  });

  $("#session").addEventListener("change", async (event) => {
    state.session = event.target.value;
    await refreshNotes();
    if (state.tab === "week") await refreshWeek();
  });

  $("#status-seg").addEventListener("click", async (event) => {
    const btn = event.target.closest(".seg-btn");
    if (!btn) return;
    state.status = btn.dataset.status;
    document.querySelectorAll("#status-seg .seg-btn").forEach((b) => b.classList.toggle("active", b === btn));
    await refreshNotes();
  });

  let searchTimer = null;
  $("#search").addEventListener("input", (event) => {
    clearTimeout(searchTimer);
    const value = event.target.value.trim();
    searchTimer = setTimeout(() => {
      state.q = value;
      refreshNotes();
    }, 240);
  });

  $("#list-body").addEventListener("click", (event) => {
    onListAction(event).catch(() => {});
  });

  $("#btn-refresh").addEventListener("click", async () => {
    await refreshOverview();
    await refreshNotes();
    if (state.tab === "week") await refreshWeek();
    toast("已刷新");
  });

  $("#btn-add").addEventListener("click", () => openModal(null));

  $("#week-prev").addEventListener("click", async () => {
    state.weekOffset -= 1;
    await refreshWeek();
  });
  $("#week-next").addEventListener("click", async () => {
    state.weekOffset += 1;
    await refreshWeek();
  });
  $("#week-today").addEventListener("click", async () => {
    state.weekOffset = 0;
    await refreshWeek();
  });

  $("#cfg-save").addEventListener("click", () => saveConfig().catch(() => {}));

  $("#modal-cancel").addEventListener("click", closeModal);
  $("#modal-mask").addEventListener("click", closeModal);
  $("#modal-save").addEventListener("click", () => submitModal().catch(() => {}));
  $("#f-pri").addEventListener("click", (event) => {
    const btn = event.target.closest(".seg-btn");
    if (!btn) return;
    state.priority = Number(btn.dataset.pri);
    syncPriority();
  });
  $("#f-content").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      submitModal().catch(() => {});
    }
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !$("#modal").classList.contains("hidden")) closeModal();
  });
}

/* ---------------------------------------------------------------- 启动 */

async function boot() {
  if (DEMO) {
    $("#demo-flag").classList.remove("hidden");
    // 预览模式支持 ?theme=dark 和 #week/#config 直达，方便单独调样式
    const params = new URLSearchParams(location.search);
    if (params.get("theme")) document.documentElement.dataset.theme = params.get("theme");
  } else {
    const context = await bridge.ready();
    if (context?.pageTitle) document.title = `随手记 · ${context.pageTitle}`;
  }
  bindEvents();
  try {
    await refreshOverview();
    await refreshNotes();
  } catch (err) {
    $("#subtitle").textContent = "加载失败，请重载插件后重试";
    toast(err?.message || "加载失败", true);
  }
  const initialTab = (location.hash || "").replace("#", "");
  if (["list", "week", "config"].includes(initialTab)) switchTab(initialTab);
}

boot();
