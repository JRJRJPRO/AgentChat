/* AgentChat 前端逻辑（无框架，直接操作 DOM）
 *
 * 状态都放在 S 里；服务器通过 WebSocket 推事件：
 *   msg / agent / chain / convs_changed / read
 */
"use strict";

const S = {
  lang: localStorage.getItem("lang") || "zh",
  theme: localStorage.getItem("theme") || "dark",
  stats: null,
  skillsInfo: null,
  tab: "chats",
  agents: [],
  convs: [],          // 我的会话摘要
  discover: [],       // 所有会话（发现页）
  cur: null,          // 当前打开的会话摘要
  spectate: false,
  msgs: [],           // 当前会话已加载消息（升序）
  msgIds: new Set(),
  hasMore: false,
  loadingOlder: false,
  working: new Set(), // 正在运行的 agent id
  defaults: {},
  models: [],
  permissions: [],
};

const $ = (id) => document.getElementById(id);
const t = (k) => (I18N[S.lang] && I18N[S.lang][k]) || k;

// ---------------- 基础工具 ----------------

async function api(path, body) {
  const opt = body !== undefined
    ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }
    : {};
  const r = await fetch(path, opt);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || r.statusText);
  return data;
}

let toastTimer = null;
function toast(msg, isErr) {
  const el = $("toast");
  el.textContent = msg;
  el.className = isErr ? "error" : "";
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), 2600);
}

function esc(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// 轻量 markdown：代码块 / 行内代码 / 粗体 / 链接。先转义再变换，防注入。
function mdlite(text) {
  const parts = text.split(/```/);
  let out = "";
  for (let i = 0; i < parts.length; i++) {
    if (i % 2 === 1) { // 代码块内部
      out += "<pre><code>" + esc(parts[i].replace(/^\w*\n/, "")) + "</code></pre>";
    } else {
      let s = esc(parts[i]);
      s = s.replace(/`([^`\n]+)`/g, "<code>$1</code>");
      s = s.replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>");
      s = s.replace(/(https?:\/\/[^\s<)]+)/g, '<a href="$1" target="_blank">$1</a>');
      out += s;
    }
  }
  return out;
}

function fmtTime(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000), now = new Date();
  const hm = d.toTimeString().slice(0, 5);
  if (d.toDateString() === now.toDateString()) return hm;
  return `${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")} ${hm}`;
}

// 聊天里的日期分隔线：今天 / 昨天 / 2026-07-01
function dayKey(ts) { return new Date(ts * 1000).toDateString(); }
function dayLabel(ts) {
  const d = new Date(ts * 1000), now = new Date();
  if (d.toDateString() === now.toDateString()) return t("today");
  const yd = new Date(now.getTime() - 864e5);
  if (d.toDateString() === yd.toDateString()) return t("yesterday");
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}
function dividerHtml(ts) { return `<div class="day-divider">${esc(dayLabel(ts))}</div>`; }

function fmtTok(n) {
  if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
  return String(n);
}

const AVATAR_COLORS = ["#4f6df5","#9256d9","#c94f7c","#c96b3b","#3f9e6e","#3a8fb7","#b3822e","#6d7f3a"];
function avatarColor(name) {
  let h = 0;
  for (const ch of name) h = (h * 31 + ch.codePointAt(0)) >>> 0;
  return AVATAR_COLORS[h % AVATAR_COLORS.length];
}
function avatarHtml(name, isUser, extraCls, dotCls) {
  const color = isUser ? "var(--bubble-me)" : avatarColor(name || "?");
  const ch = isUser ? t("me") : (name || "?").slice(0, 1);
  const dot = dotCls ? `<span class="dot ${dotCls}"></span>` : "";
  return `<div class="avatar ${extraCls || ""}" style="background:${color}">${esc(ch)}${dot}</div>`;
}

function agentById(id) { return S.agents.find((a) => a.id === id); }
function agentDot(a) {
  if (!a) return "";
  if (S.working.has(a.id) || a.run === "working") return "working";
  if (a.status === "active") return "online";
  return "paused";
}

// ---------------- i18n ----------------

function applyI18n() {
  document.querySelectorAll("[data-i18n]").forEach((el) => (el.textContent = t(el.dataset.i18n)));
  document.querySelectorAll("[data-i18n-ph]").forEach((el) => (el.placeholder = t(el.dataset.i18nPh)));
  document.documentElement.lang = S.lang;
}

// ---------------- 侧栏渲染 ----------------

function renderLists() {
  renderConvList($("listChats"), S.convs, "empty_chats", false);
  renderAgentList();
  renderConvList($("listDiscover"), S.discover, "empty_discover", true);
  const unread = S.convs.reduce((n, c) => n + (c.unread || 0), 0);
  document.title = (unread ? `(${unread}) ` : "") + "AgentChat";
}

function renderConvList(root, convs, emptyKey, isDiscover) {
  root.innerHTML = "";
  if (!convs.length) {
    root.innerHTML = `<div class="list-empty">${esc(t(emptyKey))}</div>`;
    return;
  }
  for (const c of convs) {
    const item = document.createElement("div");
    item.className = "conv-item" + (S.cur && S.cur.id === c.id ? " active" : "");
    const isDm = c.type === "dm";
    let dot = "";
    if (isDm && c.is_member) {
      const other = c.members.find((m) => m.mtype === "agent");
      if (other) dot = agentDot(agentById(other.mid));
    }
    let prev = "";
    if (c.last_msg) {
      const who = c.last_msg.stype === "system" ? "" : (c.last_msg.sender ? c.last_msg.sender + ": " : "");
      prev = who + c.last_msg.content.replace(/\s+/g, " ").slice(0, 60);
    }
    const right = [];
    right.push(`<span class="conv-time">${fmtTime(c.last_ts)}</span>`);
    if (c.unread > 0) right.push(`<span class="badge">${c.unread > 99 ? "99+" : c.unread}</span>`);
    else if (isDiscover) right.push(`<span class="tag">${t(c.is_member ? "mine_tag" : "spectate_tag")}</span>`);
    item.innerHTML =
      avatarHtml(c.display_name, false, "", dot) +
      `<div class="conv-mid"><div class="conv-name">${esc(c.display_name)}</div>` +
      `<div class="conv-prev">${esc(prev)}</div></div>` +
      `<div class="conv-right">${right.join("")}</div>`;
    item.onclick = () => openConv(c.id);
    root.appendChild(item);
  }
}

function statsCardHtml() {
  const st = S.stats, sk = S.skillsInfo;
  let inner;
  if (!st || !st.total.wakes) {
    inner = `<div class="s-row" style="color:var(--muted)">${esc(t("stats_empty"))}</div>`;
  } else {
    const T = st.total;
    inner =
      `<div class="s-row">` +
      `<span><span class="s-num">${T.wakes}</span> ${esc(t("stats_wakes"))}</span>` +
      `<span>${esc(t("stats_out"))} <span class="s-num">${fmtTok(T.output_tokens)}</span></span>` +
      `<span>${esc(t("stats_in"))} <span class="s-num">${fmtTok(T.input_tokens)}</span></span>` +
      `<span>${esc(t("stats_cache"))} <span class="s-num">${fmtTok(T.cache_read)}</span></span>` +
      `<span>${esc(t("stats_cost"))} <span class="s-num">$${T.cost_usd.toFixed(2)}</span></span></div>`;
    const per = st.per_agent.map((p) => `${esc(p.name)} ×${p.wakes} ($${p.cost_usd.toFixed(2)})`).join(" · ");
    if (per) inner += `<div class="s-agents">${per}</div>`;
  }
  let lib = "";
  if (sk) {
    lib = `<div class="lib-line">${esc(t("skills_lib"))}: ${sk.library.length} · ` +
      `${sk.global.length}${esc(t("skills_global_hint"))} ` +
      `<button class="mini-btn" data-open="lib">${esc(t("open_folder"))} skills/</button>` +
      `<button class="mini-btn" data-open="global">${esc(t("open_folder"))} ~/.claude/skills</button></div>`;
  }
  return `<div class="stats-card"><div class="s-title">${esc(t("stats_title"))}</div>${inner}${lib}</div>`;
}

async function loadStatsAndSkills() {
  const [st, sk] = await Promise.all([api("/api/stats?hours=5"), api("/api/skills")]);
  S.stats = st;
  S.skillsInfo = sk;
  renderAgentList();
}

function renderAgentList() {
  const root = $("listAgents");
  root.innerHTML = "";
  root.insertAdjacentHTML("beforeend", statsCardHtml());
  root.querySelectorAll("[data-open]").forEach((b) => {
    b.onclick = () => api("/api/open_folder", {
      path: b.dataset.open === "lib" ? S.skillsInfo.library_dir : S.skillsInfo.global_dir,
    }).catch((e) => toast(e.message, 1));
  });
  if (!S.agents.length) {
    root.insertAdjacentHTML("beforeend", `<div class="list-empty">${esc(t("empty_agents"))}</div>`);
    return;
  }
  for (const a of S.agents) {
    const card = document.createElement("div");
    card.className = "agent-card";
    const running = S.working.has(a.id);
    const stText = running ? t("working") : t(a.status === "active" ? "online" : a.status);
    card.innerHTML =
      `<div class="row1">${avatarHtml(a.name, false, "", agentDot(a))}` +
      `<div style="flex:1;min-width:0"><div class="a-name">${esc(a.name)}</div>` +
      `<div class="a-sub">${stText} · ${esc(a.model)} · ${a.wake_count}${t("wakes")}${a.memo ? " · " + esc(a.memo) : ""}</div></div></div>` +
      `<div class="a-btns"></div>`;
    const btns = card.querySelector(".a-btns");
    const addBtn = (label, cls, fn) => {
      const b = document.createElement("button");
      b.className = "mini-btn " + (cls || "");
      b.textContent = label;
      b.onclick = fn;
      btns.appendChild(b);
    };
    addBtn(t("dm"), "", async () => {
      const r = await api(`/api/agents/${a.id}/dm`, {});
      await refreshLists();
      openConv(r.conv_id);
      setTab("chats");
    });
    if (running) {
      addBtn(t("interrupt"), "warn", () => interruptAgent(a.id));
      addBtn(t("stop"), "danger", () => api(`/api/agents/${a.id}/stop`, {}).catch((e) => toast(e.message, 1)));
      btns.title = t("interrupt_hint");
    }
    if (a.status === "active") addBtn(t("pause"), "warn", () => setAgentStatus(a.id, "paused"));
    if (a.status === "paused") addBtn(t("resume"), "", () => setAgentStatus(a.id, "active"));
    if (a.status !== "archived") addBtn(t("archive"), "", () => setAgentStatus(a.id, "archived"));
    else addBtn(t("unarchive"), "", () => setAgentStatus(a.id, "paused"));
    addBtn("⚙", "", () => openAgentEdit(a));
    root.appendChild(card);
  }
}

async function setAgentStatus(aid, status) {
  await api(`/api/agents/${aid}/status`, { status }).catch((e) => toast(e.message, 1));
  await refreshLists();
}

// ---------------- 会话打开与消息 ----------------

async function openConv(cid) {
  const d = await api(`/api/convs/${cid}`);
  S.cur = d.conv;
  S.spectate = !d.conv.is_member;
  S.msgs = [];
  S.msgIds = new Set();
  $("empty").classList.add("hidden");
  $("chat").classList.remove("hidden");
  renderChatHead();
  $("msgs").innerHTML = "";
  const r = await api(`/api/convs/${cid}/messages?limit=50`);
  S.hasMore = r.has_more;
  for (const m of r.messages) pushMsg(m, false);
  renderMsgs(true);
  markRead();
  renderLists();
  renderBanner();
  renderTyping();
  $("composer").classList.toggle("hidden", S.spectate);
  $("spectateBar").classList.toggle("hidden", !S.spectate);
  if (!S.spectate) $("input").focus();
}

function renderChatHead() {
  const c = S.cur;
  $("chatTitle").textContent = c.display_name;
  $("chatSub").textContent = c.members.map((m) => (m.mtype === "user" ? t("me") : m.name)).join("、");
}

function pushMsg(m, prepend) {
  if (S.msgIds.has(m.id)) return false;
  S.msgIds.add(m.id);
  if (prepend) S.msgs.unshift(m);
  else S.msgs.push(m);
  return true;
}

function msgHtml(m) {
  if (m.stype === "system") {
    return `<div class="sys-msg ${m.kind === "error" ? "error" : ""}">${esc(m.content)} · ${fmtTime(m.created_at)}</div>`;
  }
  const mine = m.stype === "user";
  const a = m.stype === "agent" ? agentById(m.sid) : null;
  const showName = !mine && S.cur.type === "group";
  return (
    `<div class="msg-row ${mine ? "mine" : ""}" data-mid="${m.id}">` +
    avatarHtml(mine ? "" : m.sender, mine, "small", a ? "" : "") +
    `<div class="msg-body">` +
    (showName ? `<div class="msg-sender">${esc(m.sender)}</div>` : "") +
    `<div class="bubble">${mdlite(m.content)}</div>` +
    `<div class="msg-time">${fmtTime(m.created_at)}</div>` +
    `</div></div>`
  );
}

function renderMsgs(scrollBottom) {
  const box = $("msgs");
  let html = "";
  if (S.msgs.length === 0) html += `<div class="load-hint">${esc(t("no_msgs"))}</div>`;
  else if (!S.hasMore) html += `<div class="load-hint">${esc(t("no_more"))}</div>`;
  let prevDay = null;
  for (const m of S.msgs) {
    const dk = dayKey(m.created_at);
    if (dk !== prevDay) { html += dividerHtml(m.created_at); prevDay = dk; }
    html += msgHtml(m);
  }
  box.innerHTML = html;
  if (scrollBottom) box.scrollTop = box.scrollHeight;
}

function appendMsg(m) {
  const last = S.msgs[S.msgs.length - 1];
  if (!pushMsg(m, false)) return;
  const box = $("msgs");
  const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 120;
  let html = "";
  if (!last || dayKey(last.created_at) !== dayKey(m.created_at)) html += dividerHtml(m.created_at);
  box.insertAdjacentHTML("beforeend", html + msgHtml(m));
  if (nearBottom || m.stype === "user") box.scrollTop = box.scrollHeight;
}

async function loadOlder() {
  if (!S.cur || !S.hasMore || S.loadingOlder || !S.msgs.length) return;
  S.loadingOlder = true;
  const box = $("msgs");
  const oldH = box.scrollHeight;
  try {
    const r = await api(`/api/convs/${S.cur.id}/messages?before_id=${S.msgs[0].id}&limit=50`);
    S.hasMore = r.has_more;
    for (let i = r.messages.length - 1; i >= 0; i--) pushMsg(r.messages[i], true);
    renderMsgs(false);
    box.scrollTop = box.scrollHeight - oldH; // 维持视觉位置
  } finally {
    S.loadingOlder = false;
  }
}

function markRead() {
  if (!S.cur || S.spectate || !S.msgs.length) return;
  const last = S.msgs[S.msgs.length - 1].id;
  api(`/api/convs/${S.cur.id}/read`, { last_id: last }).catch(() => {});
  const c = S.convs.find((x) => x.id === S.cur.id);
  if (c) c.unread = 0;
  renderLists();
}

async function sendMsg() {
  const input = $("input");
  const text = input.value.trim();
  if (!text || !S.cur) return;
  input.value = "";
  input.style.height = "auto";
  try {
    const r = await api(`/api/convs/${S.cur.id}/send`, { text });
    appendMsg(r.message);
    markRead();
  } catch (e) {
    input.value = text;
    toast(e.message, 1);
  }
}

function renderBanner() {
  const b = $("banner");
  if (S.cur && S.cur.chain && S.cur.chain.paused) {
    b.innerHTML = `<span>⚠ ${esc(t("chain_paused"))} (${S.cur.chain.count}/${S.cur.chain.limit})</span>` +
      `<button id="btnChainReset">${esc(t("resume_chain"))}</button>`;
    b.classList.remove("hidden");
    $("btnChainReset").onclick = () => api(`/api/convs/${S.cur.id}/chain_reset`, {}).then(() => {
      S.cur.chain.paused = false;
      renderBanner();
    });
  } else {
    b.classList.add("hidden");
  }
}

async function interruptAgent(aid) {
  try {
    const r = await api(`/api/agents/${aid}/interrupt`, {});
    if (r.ok) toast(t("interrupt_done"));
  } catch (e) { toast(e.message, 1); }
}

function renderTyping() {
  const el = $("typing");
  if (!S.cur) return el.classList.add("hidden");
  const workers = S.cur.members.filter((m) => m.mtype === "agent" && S.working.has(m.mid));
  if (workers.length) {
    el.innerHTML = `${esc(workers.map((m) => m.name).join("、"))} ${esc(t("typing"))} ` +
      workers.map((m) =>
        `<button class="mini-btn warn" data-int="${m.mid}" title="${esc(t("interrupt_hint"))}">⏹ ${esc(t("interrupt"))} ${esc(m.name)}</button>`
      ).join(" ");
    el.querySelectorAll("[data-int]").forEach((b) => (b.onclick = () => interruptAgent(+b.dataset.int)));
    el.classList.remove("hidden");
  } else {
    el.classList.add("hidden");
  }
}

// ---------------- 数据刷新 ----------------

async function refreshLists() {
  const st = await api("/api/state");
  S.agents = st.agents;
  S.convs = st.convs;
  S.working = new Set(st.agents.filter((a) => a.run === "working").map((a) => a.id));
  S.models = st.models;
  S.permissions = st.permissions;
  S.defaults = st.defaults;
  if (S.tab === "discover") {
    const d = await api("/api/convs?scope=all");
    S.discover = d.convs;
  }
  if (S.cur) {
    // 同步当前会话的成员/链长信息
    try {
      const d = await api(`/api/convs/${S.cur.id}`);
      S.cur = d.conv;
      S.spectate = !d.conv.is_member;
      renderChatHead();
      renderBanner();
      $("composer").classList.toggle("hidden", S.spectate);
      $("spectateBar").classList.toggle("hidden", !S.spectate);
    } catch (e) { /* 会话可能被删 */ }
  }
  renderLists();
  renderTyping();
}

// ---------------- WebSocket ----------------

function connectWS() {
  const ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onmessage = (ev) => {
    const d = JSON.parse(ev.data);
    if (d.t === "msg") onWsMsg(d);
    else if (d.t === "agent") {
      if (d.run === "working") S.working.add(d.id);
      else S.working.delete(d.id);
      const a = agentById(d.id);
      if (a) a.run = d.run;
      renderLists();
      renderTyping();
    } else if (d.t === "chain") {
      const c = S.convs.find((x) => x.id === d.conv_id);
      if (c && c.chain) c.chain.paused = d.paused;
      if (S.cur && S.cur.id === d.conv_id) {
        S.cur.chain.paused = d.paused;
        renderBanner();
      }
    } else if (d.t === "convs_changed") {
      refreshLists();
    } else if (d.t === "read") {
      const c = S.convs.find((x) => x.id === d.conv_id);
      if (c) { c.unread = 0; renderLists(); }
    }
  };
  ws.onclose = () => setTimeout(connectWS, 2000);
  ws.onopen = () => refreshLists();
  setInterval(() => { if (ws.readyState === 1) ws.send("ping"); }, 30000);
}

function onWsMsg(d) {
  const c = S.convs.find((x) => x.id === d.conv_id);
  if (c && d.message.stype !== "user" && d.message.stype !== "system") {
    notify(d.conv_id, c.display_name, `${d.message.sender}: ${d.message.content}`);
  }
  if (c) {
    c.last_msg = d.message;
    c.last_ts = d.message.created_at;
    const viewing = S.cur && S.cur.id === d.conv_id && document.hasFocus();
    if (d.message.stype !== "user" && !viewing) c.unread += 1;
    S.convs.sort((a, b) => b.last_ts - a.last_ts);
  } else {
    refreshLists(); // 新会话（agent 建群/私聊我）
  }
  const dc = S.discover.find((x) => x.id === d.conv_id);
  if (dc) { dc.last_msg = d.message; dc.last_ts = d.message.created_at; }
  if (S.cur && S.cur.id === d.conv_id) {
    appendMsg(d.message);
    if (document.hasFocus()) markRead();
    if (S.cur.chain) { // 本地更新链长计数
      if (d.message.stype === "agent") S.cur.chain.count += 1;
      else if (d.message.stype === "user" || d.message.kind === "chain_reset") S.cur.chain.count = 0;
    }
  }
  renderLists();
}

// ---------------- 弹窗 ----------------

function openModal(id) {
  $("overlay").classList.remove("hidden");
  document.querySelectorAll(".modal").forEach((m) => m.classList.add("hidden"));
  $(id).classList.remove("hidden");
}
function closeModal() { $("overlay").classList.add("hidden"); }

function fillSelect(sel, options, value) {
  sel.innerHTML = "";
  for (const o of options) {
    const op = document.createElement("option");
    op.value = o; op.textContent = o;
    sel.appendChild(op);
  }
  if (value) sel.value = value;
}

async function renderSkillChecks(rootId, checkedNames) {
  const root = $(rootId);
  root.innerHTML = "";
  try {
    if (!S.skillsInfo) S.skillsInfo = await api("/api/skills");
    const lib = S.skillsInfo.library;
    if (!lib.length) {
      root.innerHTML = `<div class="hint" style="margin:0">${esc(t("skills_none"))}</div>`;
      return;
    }
    for (const s of lib) {
      const line = document.createElement("label");
      line.className = "check-line";
      const on = checkedNames.includes(s.name) ? "checked" : "";
      line.innerHTML = `<input type="checkbox" value="${esc(s.name)}" ${on}>` +
        `<span><b>${esc(s.name)}</b>${s.description ? " — " + esc(s.description.slice(0, 60)) : ""}</span>`;
      root.appendChild(line);
    }
    root.insertAdjacentHTML("beforeend",
      `<div class="hint" style="margin:6px 0 0">${S.skillsInfo.global.length}${esc(t("skills_global_hint"))}</div>`);
  } catch (e) { root.innerHTML = ""; }
}

function checkedSkills(rootId) {
  return [...$(rootId).querySelectorAll("input:checked")].map((x) => x.value);
}

function openNewAgent() {
  fillSelect($("agModel"), S.models, S.defaults.model);
  fillSelect($("agPerm"), S.permissions, S.defaults.permission);
  $("agName").value = ""; $("agCwd").value = ""; $("agMemo").value = ""; $("agDirs").value = "";
  updatePermHint();
  renderSkillChecks("agSkills", []);
  openModal("modalAgent");
  $("agName").focus();
}

function updatePermHint() {
  $("agPermHint").textContent = t("perm_" + $("agPerm").value);
}

async function createAgent() {
  try {
    const r = await api("/api/agents", {
      name: $("agName").value,
      model: $("agModel").value,
      permission: $("agPerm").value,
      cwd: $("agCwd").value,
      memo: $("agMemo").value,
      extra_dirs: $("agDirs").value,
      skills: checkedSkills("agSkills"),
    });
    closeModal();
    toast(t("agent_created"));
    await refreshLists();
    setTab("chats");
    openConv(r.dm_conv_id);
  } catch (e) { toast(e.message, 1); }
}

let editingAgent = null;
function openAgentEdit(a) {
  editingAgent = a;
  $("aeTitle").textContent = a.name;
  fillSelect($("aeModel"), S.models, a.model);
  fillSelect($("aePerm"), S.permissions, a.permission);
  $("aeMemo").value = a.memo || "";
  $("aeDirs").value = a.extra_dirs || "";
  $("aePermHint").textContent = t("perm_" + a.permission);
  $("aeInfo").textContent = `${t("f_cwd")}: ${a.cwd}`;
  renderSkillChecks("aeSkills", a.skills || []);
  openModal("modalAgentEdit");
}

async function saveAgentEdit() {
  try {
    await api(`/api/agents/${editingAgent.id}/update`, {
      model: $("aeModel").value,
      permission: $("aePerm").value,
      memo: $("aeMemo").value,
      extra_dirs: $("aeDirs").value,
      skills: checkedSkills("aeSkills"),
    });
    closeModal();
    refreshLists();
  } catch (e) { toast(e.message, 1); }
}

function openNewGroup() {
  $("gpName").value = "";
  $("gpIncludeMe").checked = true;
  const box = $("gpMembers");
  box.innerHTML = "";
  for (const a of S.agents.filter((x) => x.status !== "archived")) {
    const line = document.createElement("label");
    line.className = "check-line";
    line.innerHTML = `<input type="checkbox" value="${a.id}"><span>${esc(a.name)}</span>`;
    box.appendChild(line);
  }
  openModal("modalGroup");
}

async function createGroup() {
  const ids = [...$("gpMembers").querySelectorAll("input:checked")].map((x) => +x.value);
  try {
    const r = await api("/api/convs", {
      name: $("gpName").value,
      agent_ids: ids,
      include_user: $("gpIncludeMe").checked,
    });
    closeModal();
    await refreshLists();
    setTab("chats");
    openConv(r.conv_id);
  } catch (e) { toast(e.message, 1); }
}

function openConvInfo() {
  const c = S.cur;
  if (!c) return;
  $("cvNameRow").classList.toggle("hidden", c.type !== "group");
  $("cvName").value = c.name || "";
  $("cvChain").value = c.chain_limit || S.defaults.chain_limit;
  const box = $("cvMembers");
  box.innerHTML = "";
  for (const m of c.members) {
    const line = document.createElement("div");
    line.className = "member-line";
    const isUser = m.mtype === "user";
    const a = isUser ? null : agentById(m.mid);
    line.innerHTML =
      avatarHtml(isUser ? "" : m.name, isUser, "small", a ? agentDot(a) : "") +
      `<span class="m-name">${esc(isUser ? t("me") : m.name)}</span>`;
    if (!isUser && c.type === "group") {
      const rm = document.createElement("button");
      rm.className = "mini-btn danger";
      rm.textContent = t("remove");
      rm.onclick = async () => {
        await api(`/api/convs/${c.id}/members`, { remove_agent_ids: [m.mid] });
        await refreshLists();
        openConvInfo();
      };
      line.appendChild(rm);
    }
    box.appendChild(line);
  }
  const inConv = new Set(c.members.filter((m) => m.mtype === "agent").map((m) => m.mid));
  const candidates = S.agents.filter((a) => a.status !== "archived" && !inConv.has(a.id));
  $("cvAddRow").classList.toggle("hidden", c.type !== "group" || !candidates.length);
  const sel = $("cvAddSel");
  sel.innerHTML = "";
  for (const a of candidates) {
    const op = document.createElement("option");
    op.value = a.id; op.textContent = a.name;
    sel.appendChild(op);
  }
  $("cvLeave").classList.toggle("hidden", !(c.type === "group" && c.is_member));
  openModal("modalConv");
}

// ---------------- Tab 切换 ----------------

function setTab(tab) {
  S.tab = tab;
  document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  $("listChats").classList.toggle("hidden", tab !== "chats");
  $("listAgents").classList.toggle("hidden", tab !== "agents");
  $("listDiscover").classList.toggle("hidden", tab !== "discover");
  if (tab === "discover") {
    api("/api/convs?scope=all").then((d) => { S.discover = d.convs; renderLists(); });
  }
  if (tab === "agents") loadStatsAndSkills().catch(() => {});
}

// ---------------- 桌面通知（窗口不在前台时弹） ----------------

function notify(convId, title, body) {
  if (document.hasFocus() || !("Notification" in window)) return;
  if (Notification.permission === "default") { Notification.requestPermission(); return; }
  if (Notification.permission !== "granted") return;
  const n = new Notification(title, { body: body.slice(0, 120), tag: "conv" + convId });
  n.onclick = () => { window.focus(); openConv(convId); n.close(); };
}

// ---------------- 事件绑定 ----------------

function bind() {
  document.querySelectorAll(".tab").forEach((b) => (b.onclick = () => setTab(b.dataset.tab)));

  $("btnNew").onclick = (e) => { e.stopPropagation(); $("newMenu").classList.toggle("hidden"); };
  document.addEventListener("click", () => $("newMenu").classList.add("hidden"));
  document.querySelectorAll(".menu-item").forEach((mi) => {
    mi.onclick = () => {
      $("newMenu").classList.add("hidden");
      if (mi.dataset.act === "new-agent") openNewAgent();
      else openNewGroup();
    };
  });

  $("btnLang").onclick = () => {
    S.lang = S.lang === "zh" ? "en" : "zh";
    localStorage.setItem("lang", S.lang);
    applyI18n();
    renderLists();
    if (S.cur) { renderChatHead(); renderMsgs(false); renderBanner(); renderTyping(); }
  };

  $("btnTheme").onclick = () => {
    S.theme = S.theme === "dark" ? "light" : "dark";
    localStorage.setItem("theme", S.theme);
    document.body.classList.toggle("light", S.theme === "light");
  };

  $("btnPower").onclick = async () => {
    if (!confirm(t("shutdown_confirm"))) return;
    try { await api("/api/shutdown", {}); } catch (e) { /* 连接断开是预期的 */ }
    toast(t("server_down"));
  };

  $("aePerm").onchange = () => { $("aePermHint").textContent = t("perm_" + $("aePerm").value); };

  if ("Notification" in window && Notification.permission === "default") {
    document.addEventListener("click", () => Notification.requestPermission(), { once: true });
  }

  const input = $("input");
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      sendMsg();
    }
  });
  input.addEventListener("input", () => {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 160) + "px";
  });
  $("btnSend").onclick = sendMsg;

  $("msgs").addEventListener("scroll", () => {
    if ($("msgs").scrollTop < 60) loadOlder();
  });

  window.addEventListener("focus", () => markRead());

  $("btnConvInfo").onclick = openConvInfo;
  $("btnJoin").onclick = async () => {
    await api(`/api/convs/${S.cur.id}/members`, { join_user: true });
    openConv(S.cur.id);
    refreshLists();
  };

  // 弹窗
  $("overlay").addEventListener("click", (e) => { if (e.target === $("overlay")) closeModal(); });
  document.querySelectorAll("[data-close]").forEach((b) => (b.onclick = closeModal));
  $("agCreate").onclick = createAgent;
  $("agPerm").onchange = updatePermHint;
  $("aeSave").onclick = saveAgentEdit;
  $("gpCreate").onclick = createGroup;
  $("cvSave").onclick = async () => {
    try {
      await api(`/api/convs/${S.cur.id}/settings`, {
        name: $("cvName").value,
        chain_limit: parseInt($("cvChain").value) || null,
      });
      closeModal();
      refreshLists();
    } catch (e) { toast(e.message, 1); }
  };
  $("cvAddBtn").onclick = async () => {
    const v = +$("cvAddSel").value;
    if (!v) return;
    await api(`/api/convs/${S.cur.id}/members`, { add_agent_ids: [v] });
    await refreshLists();
    openConvInfo();
  };
  $("cvLeave").onclick = async () => {
    if (!confirm(t("confirm_leave"))) return;
    await api(`/api/convs/${S.cur.id}/members`, { leave_user: true });
    closeModal();
    S.cur = null;
    $("chat").classList.add("hidden");
    $("empty").classList.remove("hidden");
    refreshLists();
  };
}

// ---------------- 启动 ----------------

(async function init() {
  document.body.classList.toggle("light", S.theme === "light");
  applyI18n();
  bind();
  await refreshLists();
  connectWS();
})();
