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
  memsInfo: null,
  library: null,      // 资源库数据 {memories, skills}
  lib: null,          // 当前打开的包 {kind, pack}
  libFile: null,
  libDirty: false,
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
  acts: {},           // agent_id -> 本轮过程动态（思考/工具调用，只展示不进记录）
  pendAtts: [],       // 待发送附件 [{kind,name,path,url,size}]
  auth: null,         // 登录失效信息（null = 正常）
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
    let dot = "", archived = false;
    if (isDm) {
      const other = c.members.find((m) => m.mtype === "agent");
      const ag = other ? agentById(other.mid) : null;
      if (ag && ag.status === "archived") archived = true;
      else if (ag && c.is_member) dot = agentDot(ag);
    }
    let prev = "";
    if (c.last_msg) {
      const who = c.last_msg.stype === "system" ? "" : (c.last_msg.sender ? c.last_msg.sender + ": " : "");
      const body = c.last_msg.content || ((c.last_msg.attachments || []).length ? "📎 " + t("att_file") : "");
      prev = who + body.replace(/\s+/g, " ").slice(0, 60);
    }
    const right = [];
    right.push(`<span class="conv-time">${fmtTime(c.last_ts)}</span>`);
    if (c.unread > 0) right.push(`<span class="badge">${c.unread > 99 ? "99+" : c.unread}</span>`);
    else if (isDiscover) right.push(`<span class="tag">${t(c.is_member ? "mine_tag" : "spectate_tag")}</span>`);
    item.innerHTML =
      avatarHtml(c.display_name, false, (isDm ? "round" : "") + (archived ? " archived" : ""), dot) +
      `<div class="conv-mid"><div class="conv-name">${esc(c.display_name)}</div>` +
      `<div class="conv-prev">${esc(prev)}</div></div>` +
      `<div class="conv-right">${right.join("")}</div>`;
    item.onclick = () => openConv(c.id);
    root.appendChild(item);
  }
}

function renderAgentList() {
  const root = $("listAgents");
  root.innerHTML = "";
  if (!S.agents.length) {
    root.innerHTML = `<div class="list-empty">${esc(t("empty_agents"))}</div>`;
    return;
  }
  const ordered = [...S.agents].sort((x, y) =>
    (x.status === "archived" ? 1 : 0) - (y.status === "archived" ? 1 : 0));
  for (const a of ordered) {
    const card = document.createElement("div");
    card.className = "agent-card" + (a.status === "archived" ? " archived" : "");
    const running = S.working.has(a.id);
    const stText = running ? t("working") : t(a.status === "active" ? "online" : a.status);
    const av = a.status === "archived"
      ? avatarHtml(a.name, false, "round archived", "")
      : avatarHtml(a.name, false, "round", agentDot(a));
    card.innerHTML =
      `<div class="row1">${av}` +
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

function saveDraft(cid) {
  const v = $("input").value;
  if (v.trim()) localStorage.setItem("draft:" + cid, v);
  else localStorage.removeItem("draft:" + cid);
}

async function openConv(cid) {
  if (S.lib && !libConfirmDiscard()) return;
  if (S.cur && S.cur.id !== cid) saveDraft(S.cur.id);
  S.lib = null; S.libFile = null; S.libDirty = false; S.libSplit = null;
  $("lib").classList.add("hidden");
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
  S.pendAtts = [];
  renderAttachBar();
  renderTyping();
  // 有成员正在干活的话，补拉它本轮的过程动态（刷新页面/中途进来也能看到）
  for (const m of d.conv.members) {
    if (m.mtype === "agent" && S.working.has(m.mid)) fetchActs(m.mid);
  }
  $("composer").classList.toggle("hidden", S.spectate);
  $("spectateBar").classList.toggle("hidden", !S.spectate);
  const input = $("input");
  input.value = localStorage.getItem("draft:" + cid) || "";
  input.style.height = "auto";
  if (input.value) input.style.height = Math.min(input.scrollHeight, 160) + "px";
  if (!S.spectate) input.focus();
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

function fmtSize(n) {
  if (n >= 1048576) return (n / 1048576).toFixed(1) + "MB";
  if (n >= 1024) return Math.round(n / 1024) + "KB";
  return n + "B";
}

function attsHtml(atts) {
  let out = "";
  for (const a of atts || []) {
    if (a.kind === "image") {
      out += `<img class="msg-img" src="${esc(a.url)}" alt="${esc(a.name)}" onclick="window.open('${esc(a.url)}','_blank')">`;
    } else {
      out += `<a class="file-att" href="${esc(a.url)}" target="_blank">📄 ${esc(a.name)} · ${fmtSize(a.size || 0)}</a>`;
    }
  }
  return out;
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
    avatarHtml(mine ? "" : m.sender, mine, "small round", a ? "" : "") +
    `<div class="msg-body">` +
    (showName ? `<div class="msg-sender">${esc(m.sender)}</div>` : "") +
    `<div class="bubble">${mdlite(m.content)}${attsHtml(m.attachments)}</div>` +
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
  const atts = S.pendAtts.slice();
  if ((!text && !atts.length) || !S.cur) return;
  input.value = "";
  input.style.height = "auto";
  S.pendAtts = [];
  renderAttachBar();
  try {
    const r = await api(`/api/convs/${S.cur.id}/send`, { text, attachments: atts });
    localStorage.removeItem("draft:" + S.cur.id);
    appendMsg(r.message);
    markRead();
  } catch (e) {
    input.value = text;
    S.pendAtts = atts;
    renderAttachBar();
    toast(e.message, 1);
  }
}

// ---------------- 附件（拖拽图片 / 粘贴长文本自动转临时文档） ----------------

function renderAttachBar() {
  const bar = $("attachBar");
  if (!S.pendAtts.length) { bar.classList.add("hidden"); bar.innerHTML = ""; return; }
  bar.classList.remove("hidden");
  bar.innerHTML = "";
  S.pendAtts.forEach((a, i) => {
    const chip = document.createElement("div");
    chip.className = "att-chip";
    const icon = a.kind === "image" ? `<img src="${esc(a.url)}" alt="">` : "📄";
    chip.innerHTML = `${icon}<span class="att-name">${esc(a.name)}</span>` +
      `<span class="att-sz">${fmtSize(a.size || 0)}</span><span class="att-x" title="移除">✕</span>`;
    chip.querySelector(".att-x").onclick = () => { S.pendAtts.splice(i, 1); renderAttachBar(); };
    bar.appendChild(chip);
  });
}

async function uploadFile(file) {
  if (!S.cur || S.spectate) return;
  if (file.size > 30 * 1024 * 1024) { toast(t("upload_fail") + ": >30MB", 1); return; }
  toast(t("uploading"));
  try {
    const b64 = await new Promise((res, rej) => {
      const rd = new FileReader();
      rd.onload = () => res(rd.result.split(",", 2)[1] || "");
      rd.onerror = rej;
      rd.readAsDataURL(file);
    });
    const name = file.name && file.name !== "image.png" ? file.name
      : `${t("att_image")}_${new Date().toISOString().slice(0, 19).replace(/[:T-]/g, "")}.png`;
    const r = await api(`/api/convs/${S.cur.id}/upload`, { name, data: b64 });
    S.pendAtts.push(r.attachment);
    renderAttachBar();
  } catch (e) { toast(t("upload_fail") + ": " + e.message, 1); }
}

async function uploadPastedText(text) {
  if (!S.cur || S.spectate) return;
  try {
    const name = `${t("att_doc_name")}_${new Date().toISOString().slice(5, 16).replace(/[:T]/g, "")}.md`;
    const r = await api(`/api/convs/${S.cur.id}/upload`, { name, text });
    S.pendAtts.push(r.attachment);
    renderAttachBar();
    toast(t("paste_as_doc"));
  } catch (e) { toast(t("upload_fail") + ": " + e.message, 1); }
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
  renderActs();
}

// ---------------- 过程动态（正在思考/读写文件/跑命令，只展示不进聊天记录） ----------------

function toolLabel(name) {
  const short = name.startsWith("mcp__chat__") ? name.slice(11) : name;
  const k = "tool_" + short;
  return (I18N[S.lang] && I18N[S.lang][k]) || short;
}

function actLineHtml(it) {
  if (it.k === "note") {
    return `<div class="act-line"><span>💭</span><span class="a-note">${esc(it.text)}</span></div>`;
  }
  return `<div class="act-line"><span class="a-tool">▸ ${esc(toolLabel(it.tool))}</span>` +
    `<span class="a-detail">${esc(it.detail || "")}</span></div>`;
}

function renderActs() {
  const el = $("actFeed");
  if (!S.cur) return el.classList.add("hidden");
  const workers = S.cur.members.filter((m) => m.mtype === "agent" && S.working.has(m.mid));
  let html = "";
  for (const m of workers) {
    const items = S.acts[m.mid] || [];
    if (!items.length) continue;
    html += `<div class="act-head">${esc(m.name)} · ${esc(t("act_title"))}</div>`;
    html += items.slice(-30).map(actLineHtml).join("");
  }
  if (!html) return el.classList.add("hidden");
  const stick = el.classList.contains("hidden") || el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  el.innerHTML = html;
  el.classList.remove("hidden");
  if (stick) el.scrollTop = el.scrollHeight;
}

async function fetchActs(aid) {
  try {
    const r = await api(`/api/agents/${aid}/activity`);
    if (r.items && r.items.length) { S.acts[aid] = r.items; renderActs(); }
  } catch (e) { /* 静默 */ }
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
  S.auth = st.auth || null;
  renderAuthBar();
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
      if (d.run === "working") { S.working.add(d.id); S.acts[d.id] = []; }
      else S.working.delete(d.id);
      const a = agentById(d.id);
      if (a) a.run = d.run;
      renderLists();
      renderTyping();
    } else if (d.t === "act") {
      (S.acts[d.id] = S.acts[d.id] || []).push(d.item);
      if (S.acts[d.id].length > 100) S.acts[d.id].splice(0, S.acts[d.id].length - 100);
      renderActs();
    } else if (d.t === "auth") {
      S.auth = d.needed ? d : null;
      renderAuthBar();
      if (!d.needed) toast(t("auth_recovered"));
    } else if (d.t === "ask") {
      addAskCard(d.req);
    } else if (d.t === "ask_done") {
      removeAskCard(d.id);
      if (d.reason === "timeout") toast(`${d.agent || ""}: ${t("ask_timeout")}`, 1);
    } else if (d.t === "ask_hold") {
      const card = $("ask" + d.id);
      if (card) {
        card.dataset.exp = d.expires_at;
        card.dataset.held = "1";
        card.querySelector(".ask-hold").classList.add("hidden");
        updateAskTimers();
      }
    } else if (d.t === "chain") {
      const c = S.convs.find((x) => x.id === d.conv_id);
      if (c && c.chain) c.chain.paused = d.paused;
      if (S.cur && S.cur.id === d.conv_id) {
        S.cur.chain.paused = d.paused;
        renderBanner();
      }
    } else if (d.t === "perm") {
      addPermCard(d.req);
    } else if (d.t === "perm_done") {
      const card = $("perm" + d.id);
      if (card) card.remove();
    } else if (d.t === "convs_changed") {
      refreshLists();
    } else if (d.t === "read") {
      const c = S.convs.find((x) => x.id === d.conv_id);
      if (c) { c.unread = 0; renderLists(); }
    }
  };
  ws.onclose = () => setTimeout(connectWS, 2000);
  ws.onopen = () => { refreshLists(); loadPendingPerms(); loadPendingAsks(); };
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
  // 清掉上次遗留在 root 旁边的全局技能说明，避免重复叠加
  while (root.nextElementSibling && root.nextElementSibling.classList.contains("skill-note")) {
    root.nextElementSibling.remove();
  }
  try {
    if (!S.skillsInfo) S.skillsInfo = await api("/api/skills");
    const lib = S.skillsInfo.library;
    if (!lib.length) {
      root.insertAdjacentHTML("afterend", `<div class="skill-note">${esc(t("skills_none"))}</div>`);
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
    root.insertAdjacentHTML("afterend",
      `<div class="skill-note">${S.skillsInfo.global.length}${esc(t("skills_global_hint"))}</div>`);
  } catch (e) { root.innerHTML = ""; }
}

function checkedSkills(rootId) {
  return [...$(rootId).querySelectorAll("input:checked")].map((x) => x.value);
}

async function renderMemChecks(rootId, checkedNames) {
  const root = $(rootId);
  root.innerHTML = "";
  while (root.nextElementSibling && root.nextElementSibling.classList.contains("skill-note")) {
    root.nextElementSibling.remove();
  }
  try {
    if (!S.memsInfo) S.memsInfo = await api("/api/memories");
    const lib = S.memsInfo.library;
    if (!lib.length) {
      root.insertAdjacentHTML("afterend", `<div class="skill-note">${esc(t("memories_none"))}</div>`);
      return;
    }
    for (const m of lib) {
      const line = document.createElement("label");
      line.className = "check-line";
      const on = checkedNames.includes(m.name) ? "checked" : "";
      const n = Math.max(0, (m.files || []).length - 1);  // 除去 MEMORY.md 的条目数，一眼看出哪个包太肥
      line.innerHTML = `<input type="checkbox" value="${esc(m.name)}" ${on}>` +
        `<span><b>${esc(m.name)}</b>（${n}${esc(t("entries_n"))}）` +
        `${m.description ? " — " + esc(m.description.slice(0, 60)) : ""}</span>`;
      root.appendChild(line);
    }
    root.insertAdjacentHTML("afterend", `<div class="skill-note">${esc(t("memories_hint"))}</div>`);
  } catch (e) { root.innerHTML = ""; }
}

function openNewAgent() {
  fillSelect($("agModel"), S.models, S.defaults.model);
  fillSelect($("agPerm"), S.permissions, S.defaults.permission);
  $("agName").value = ""; $("agCwd").value = ""; $("agMemo").value = ""; $("agDirs").value = "";
  $("agAsk").checked = false;
  updatePermHint();
  renderSkillChecks("agSkills", []);
  renderMemChecks("agMems", []);
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
      ask_perm: $("agAsk").checked,
      skills: checkedSkills("agSkills"),
      memories: checkedSkills("agMems"),
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
  $("aeAsk").checked = !!a.ask_perm;
  $("aePermHint").textContent = t("perm_" + a.permission);
  $("aeInfo").textContent = `${t("f_cwd")}: ${a.cwd}`;
  $("aeInfo").className = "info-line";
  renderSkillChecks("aeSkills", a.skills || []);
  renderMemChecks("aeMems", a.memories || []);
  openModal("modalAgentEdit");
}

async function saveAgentEdit() {
  try {
    await api(`/api/agents/${editingAgent.id}/update`, {
      model: $("aeModel").value,
      permission: $("aePerm").value,
      memo: $("aeMemo").value,
      extra_dirs: $("aeDirs").value,
      ask_perm: $("aeAsk").checked,
      skills: checkedSkills("aeSkills"),
      memories: checkedSkills("aeMems"),
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
      avatarHtml(isUser ? "" : m.name, isUser, "small round", a ? agentDot(a) : "") +
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
  $("listLibrary").classList.toggle("hidden", tab !== "library");
  if (tab === "discover") {
    api("/api/convs?scope=all").then((d) => { S.discover = d.convs; renderLists(); });
  }
  if (tab === "library") loadLibrary();
}

// ---------------- 资源库（记忆/技能的查看与编辑） ----------------

async function loadLibrary() {
  try {
    S.library = await api("/api/library");
    renderLibraryList();
  } catch (e) { toast(e.message, 1); }
}

function libPackInfo() {
  if (!S.lib || !S.library) return null;
  return (S.library[S.lib.kind] || []).find((p) => p.name === S.lib.pack) || null;
}

function libConfirmDiscard() {
  return !S.libDirty || confirm(t("discard_confirm"));
}

function renderLibraryList() {
  const root = $("listLibrary");
  root.innerHTML = "";
  if (!S.library) return;
  root.insertAdjacentHTML("beforeend", `<div class="lib-note">${esc(t("lib_copy_note"))}</div>`);
  for (const kind of ["memories", "skills"]) {
    const sect = document.createElement("div");
    sect.className = "lib-sect";
    sect.innerHTML = `<span>${esc(t(kind === "memories" ? "lib_memories" : "lib_skills"))}</span>` +
      `<button class="mini-btn">${esc(t("new_pack"))}</button>`;
    sect.querySelector("button").onclick = () => newLibPack(kind);
    root.appendChild(sect);
    const packs = S.library[kind] || [];
    if (!packs.length) {
      root.insertAdjacentHTML("beforeend", `<div class="lib-note">${esc(t("empty_library"))}</div>`);
      continue;
    }
    for (const p of packs) {
      const item = document.createElement("div");
      item.className = "lib-item" + (S.lib && S.lib.kind === kind && S.lib.pack === p.name ? " active" : "");
      const used = p.used_by && p.used_by.length
        ? `<span class="in-use">${esc(t("used_by") + p.used_by.join("、"))}</span>`
        : esc(t("unused"));
      item.innerHTML = `<div class="lib-name">${esc(p.name)}</div>` +
        (p.description ? `<div class="lib-desc">${esc(p.description)}</div>` : "") +
        `<div class="lib-used">${used} · ${p.files.length}${esc(t("files_n"))}</div>`;
      item.onclick = () => openLibPack(kind, p.name);
      root.appendChild(item);
    }
  }
}

async function openLibPack(kind, pack) {
  if (!libConfirmDiscard()) return;
  S.lib = { kind, pack };
  S.libFile = null;
  S.libDirty = false;
  S.libSplit = null;
  S.cur = null;
  $("empty").classList.add("hidden");
  $("chat").classList.add("hidden");
  $("lib").classList.remove("hidden");
  renderLibraryList();
  renderLibView();
  const info = libPackInfo();
  const def = kind === "memories" ? "MEMORY.md" : "SKILL.md";
  const file = info && info.files.length ? (info.files.includes(def) ? def : info.files[0]) : null;
  if (file) openLibFile(file, true);
}

function renderLibView() {
  const info = libPackInfo();
  $("libTitle").textContent = S.lib ? S.lib.pack : "";
  $("libSub").textContent = !info ? "" :
    t(S.lib.kind === "memories" ? "lib_memories" : "lib_skills") + " · " +
    (info.used_by && info.used_by.length ? t("used_by") + info.used_by.join("、") : t("unused"));
  const box = $("libFiles");
  box.innerHTML = "";
  for (const f of (info ? info.files : [])) {
    const chip = document.createElement("button");
    if (S.libSplit) {  // 拆分模式：点条目是勾选，MEMORY.md（索引本体）不能拆走
      const pickable = f !== "MEMORY.md";
      chip.className = "file-chip" + (S.libSplit.has(f) ? " selected" : "") + (pickable ? "" : " dim");
      chip.onclick = () => {
        if (!pickable) return;
        S.libSplit.has(f) ? S.libSplit.delete(f) : S.libSplit.add(f);
        renderLibView();
      };
    } else {
      chip.className = "file-chip" + (f === S.libFile ? " active" : "");
      chip.onclick = () => openLibFile(f);
    }
    chip.textContent = f;
    box.appendChild(chip);
  }
  const sp = $("libSplit");
  sp.classList.toggle("hidden", !(S.lib && S.lib.kind === "memories"));
  sp.textContent = !S.libSplit ? t("split_pack")
    : (S.libSplit.size ? t("split_out").replace("{n}", S.libSplit.size) : t("split_cancel"));
  if (S.libSplit) $("libStatus").textContent = t("split_hint");
  else if (!S.libFile) {
    $("libEditor").value = "";
    $("libStatus").textContent = t("select_file");
  }
}

async function libSplitClick() {
  if (!S.libSplit) {              // 进入拆分模式
    S.libSplit = new Set();
    renderLibView();
    return;
  }
  if (!S.libSplit.size) {         // 空选再点一次 = 取消
    S.libSplit = null;
    renderLibView();
    return;
  }
  const name = (prompt(t("split_name_prompt")) || "").trim();
  if (!name) return;
  const desc = (prompt(t("split_desc_prompt")) || "").trim();
  try {
    await api("/api/library/split", {
      kind: "memories", pack: S.lib.pack, files: [...S.libSplit],
      new_name: name, description: desc,
    });
    S.libSplit = null;
    S.libFile = null;
    S.memsInfo = null;            // 勾选列表缓存失效
    await loadLibrary();
    openLibPack("memories", name);
    toast(t("split_done"));
  } catch (e) { toast(e.message, 1); }
}

async function openLibFile(f, force) {
  if (!force && !libConfirmDiscard()) return;
  try {
    const r = await api("/api/library/read", { kind: S.lib.kind, pack: S.lib.pack, file: f });
    S.libFile = f;
    S.libDirty = false;
    renderLibView();
    $("libEditor").value = r.content;
    $("libStatus").textContent = "";
  } catch (e) { toast(e.message, 1); }
}

async function saveLibFile() {
  if (!S.lib || !S.libFile) return;
  try {
    await api("/api/library/save", {
      kind: S.lib.kind, pack: S.lib.pack, file: S.libFile,
      content: $("libEditor").value,
    });
    S.libDirty = false;
    $("libStatus").textContent = t("saved");
    S.memsInfo = S.skillsInfo = null; // 描述可能变了，下次弹窗重新拉
    const keep = S.libFile;
    await loadLibrary();
    S.libFile = keep;
    renderLibView();
  } catch (e) { toast(e.message, 1); }
}

async function newLibPack(kind) {
  const name = (prompt(t("pack_name_prompt")) || "").trim();
  if (!name) return;
  try {
    await api("/api/library/new_pack", { kind, name });
    S.memsInfo = S.skillsInfo = null;
    await loadLibrary();
    openLibPack(kind, name);
  } catch (e) { toast(e.message, 1); }
}

async function newLibFile() {
  if (!S.lib || !libConfirmDiscard()) return;
  const name = (prompt(t("file_name_prompt")) || "").trim();
  if (!name) return;
  try {
    const r = await api("/api/library/new_file", { kind: S.lib.kind, pack: S.lib.pack, name });
    await loadLibrary();
    renderLibView();
    openLibFile(r.file, true);
  } catch (e) { toast(e.message, 1); }
}

// ---------------- 设置弹窗 ----------------

function usageBarHtml(label, pct, resetText) {
  const cls = pct >= 90 ? "danger" : pct >= 70 ? "warn" : "";
  return `<div class="u-row"><span class="u-label">${esc(label)}</span>` +
    `<div class="u-bar"><div class="u-fill ${cls}" style="width:${Math.min(100, pct)}%"></div></div>` +
    `<span class="u-pct">${Math.round(pct)}%</span><span class="u-reset">${esc(resetText)}</span></div>`;
}

function fmtReset(iso) {
  if (!iso) return "";
  const ms = new Date(iso).getTime() - Date.now();
  if (isNaN(ms) || ms <= 0) return "";
  const h = ms / 36e5;
  const txt = h < 1 ? `${Math.round(ms / 6e4)}m` : h < 48 ? `${Math.round(h)}h` : `${Math.round(h / 24)}d`;
  return S.lang === "zh" ? `${txt}${t("resets_in")}` : `${txt}${t("resets_in")}`;
}

async function openSettings() {
  $("stTheme").value = S.theme;
  $("stLang").value = S.lang;
  $("stUsage").innerHTML = $("stLocal").innerHTML = $("stSkills").innerHTML =
    `<span class="muted">…</span>`;
  openModal("modalSettings");
  try {
    const [u, sk] = await Promise.all([api("/api/usage"), api("/api/skills")]);
    S.skillsInfo = sk;
    // 订阅用量（和 /usage 命令同源）
    if (u.subscription && u.subscription.length) {
      $("stUsage").innerHTML = u.subscription
        .map((w) => usageBarHtml(w.label, w.utilization || 0, fmtReset(w.resets_at))).join("");
    } else {
      $("stUsage").innerHTML = `<span class="muted">${esc(t("usage_unavailable"))}</span>`;
    }
    // 本系统自身消耗
    const T = u.local.total;
    if (!T.wakes) {
      $("stLocal").innerHTML = `<span class="muted">${esc(t("stats_empty"))}</span>`;
    } else {
      const per = u.local.per_agent.map((p) => `${esc(p.name)} ×${p.wakes}`).join(" · ");
      $("stLocal").innerHTML =
        `${T.wakes} ${t("stats_wakes")} · ${t("stats_out")} ${fmtTok(T.output_tokens)} · ` +
        `${t("stats_in")} ${fmtTok(T.input_tokens)} · ${t("stats_cache")} ${fmtTok(T.cache_read)}` +
        `<div class="muted">${per}</div>`;
    }
    // 技能库入口
    $("stSkills").innerHTML =
      `<span class="muted">${sk.library.length} · ${sk.global.length}${esc(t("skills_global_hint"))}</span>` +
      `<div style="margin-top:6px;display:flex;gap:8px;flex-wrap:wrap">` +
      `<button class="mini-btn" data-open="lib">${esc(t("open_folder"))} skills/</button>` +
      `<button class="mini-btn" data-open="global">${esc(t("open_folder"))} ~/.claude/skills</button></div>`;
    $("stSkills").querySelectorAll("[data-open]").forEach((b) => {
      b.onclick = () => api("/api/open_folder", {
        path: b.dataset.open === "lib" ? sk.library_dir : sk.global_dir,
      }).catch((e) => toast(e.message, 1));
    });
  } catch (e) {
    $("stUsage").innerHTML = `<span class="muted">${esc(e.message)}</span>`;
  }
}

// ---------------- 越权授权请求 ----------------

function permCardHtml(r) {
  return `<div class="perm-card" id="perm${r.id}">` +
    `<div class="p-title">⚠ ${esc(r.agent)} ${esc(t("perm_title"))}</div>` +
    `<div class="p-tool">${esc(r.tool)}</div>` +
    `<div class="p-input">${esc(r.input_summary || "")}</div>` +
    `<div class="p-btns"><button class="deny">${esc(t("deny"))}</button>` +
    `<button class="allow">${esc(t("allow"))}</button></div></div>`;
}

function addPermCard(r) {
  if ($("perm" + r.id)) return;
  $("permPanel").insertAdjacentHTML("beforeend", permCardHtml(r));
  const card = $("perm" + r.id);
  card.querySelector(".allow").onclick = () => answerPerm(r.id, true);
  card.querySelector(".deny").onclick = () => answerPerm(r.id, false);
  notify(0, `${r.agent} ${t("perm_title")}`, r.tool);
}

async function answerPerm(rid, allow) {
  try { await api(`/api/permissions/${rid}/answer`, { allow }); } catch (e) { toast(e.message, 1); }
  const card = $("perm" + rid);
  if (card) card.remove();
}

async function loadPendingPerms() {
  try {
    const d = await api("/api/permissions");
    for (const r of d.pending) addPermCard(r);
  } catch (e) { /* 静默 */ }
}

// ---------------- agent 提问选择卡（ask_user） ----------------

function addAskCard(r) {
  if ($("ask" + r.id)) return;
  const card = document.createElement("div");
  card.className = "ask-card";
  card.id = "ask" + r.id;
  card.dataset.exp = r.expires_at || Date.now() / 1000 + 600;  // 兜底：老服务器没给截止时刻
  if (r.held) card.dataset.held = "1";
  card.innerHTML =
    `<div class="p-title"><span class="ask-timer"></span>` +
    `<button class="ask-hold${r.held ? " hidden" : ""}">⏸ ${esc(t("ask_hold"))}</button>` +
    `💬 ${esc(r.agent)} · ${esc(t("ask_title"))}</div>` +
    `<div class="ask-q">${esc(r.question)}</div>` +
    `<div class="ask-opts">` +
    r.options.map((o, i) => `<button class="ask-opt" data-i="${i}">${esc(o)}</button>`).join("") +
    `</div>` +
    `<div class="ask-custom"><input placeholder="${esc(t("ask_custom_ph"))}">` +
    `<button>${esc(t("ask_send"))}</button></div>`;
  card.querySelectorAll(".ask-opt").forEach((b) =>
    (b.onclick = () => answerAsk(r.id, r.options[+b.dataset.i])));
  card.querySelector(".ask-hold").onclick = async () => {
    try { await api(`/api/asks/${r.id}/hold`, {}); } catch (e) { toast(e.message, 1); }
  };
  const inp = card.querySelector(".ask-custom input");
  const send = () => { if (inp.value.trim()) answerAsk(r.id, inp.value.trim()); };
  card.querySelector(".ask-custom button").onclick = send;
  inp.addEventListener("keydown", (e) => { if (e.key === "Enter") send(); });
  $("askList").appendChild(card);
  S.askCollapsed = false;  // 新提问来了自动展开，别让人错过
  renderAskOverlay();
  notify(0, `${r.agent} ${t("ask_title")}`, r.question);
}

function removeAskCard(id) {
  const card = $("ask" + id);
  if (card) card.remove();
  renderAskOverlay();
}

async function answerAsk(rid, answer) {
  try { await api(`/api/asks/${rid}/answer`, { answer }); } catch (e) { toast(e.message, 1); }
  removeAskCard(rid);
}

// 浮层随卡片数量显隐：0 个全藏；收起时只留底部小徽标
let askTick = null;
function renderAskOverlay() {
  const n = $("askList").children.length;
  const ov = $("askOverlay"), badge = $("askBadge");
  if (!n) {
    ov.classList.add("hidden");
    badge.classList.add("hidden");
    clearInterval(askTick); askTick = null;
    return;
  }
  $("askCount").textContent = t("ask_pending_n").replace("{n}", n);
  $("askCollapse").textContent = t("ask_collapse");
  badge.textContent = `💬 ${n}`;
  ov.classList.toggle("hidden", !!S.askCollapsed);
  badge.classList.toggle("hidden", !S.askCollapsed);
  updateAskTimers();
  if (!askTick) askTick = setInterval(updateAskTimers, 1000);
}

function updateAskTimers() {
  document.querySelectorAll("#askList .ask-card").forEach((c) => {
    const el = c.querySelector(".ask-timer");
    if (c.dataset.held) {  // 已取消倒计时：不再逼人
      el.textContent = `∞ ${t("ask_held")}`;
      el.classList.remove("urgent");
      return;
    }
    const left = Math.max(0, Math.round(+c.dataset.exp - Date.now() / 1000));
    el.textContent = `⏳ ${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`;
    el.classList.toggle("urgent", left < 60);
  });
}

async function loadPendingAsks() {
  try {
    const d = await api("/api/asks");
    for (const r of d.pending) addAskCard(r);
  } catch (e) { /* 静默 */ }
}

// ---------------- Claude 登录失效提示条 ----------------

function renderAuthBar() {
  const bar = $("authBar");
  if (!S.auth) return bar.classList.add("hidden");
  const limit = S.auth.kind === "limit";  // 订阅用量打满：不用重新登录，等重置后重试即可
  $("authText").textContent = `⚠ ${t(limit ? "limit_needed" : "auth_needed")}` +
    (S.auth.agent ? `（${S.auth.agent}: ${S.auth.detail || ""}）` : "");
  $("btnAuthLogin").classList.toggle("hidden", limit);
  $("btnAuthRetry").textContent = limit ? t("retry") : t("auth_retry");
  bar.classList.remove("hidden");
}

// ---------------- 桌面通知（窗口不在前台时弹） ----------------

function notify(convId, title, body) {
  if (document.hasFocus() || !("Notification" in window)) return;
  if (Notification.permission === "default") { Notification.requestPermission(); return; }
  if (Notification.permission !== "granted") return;
  const n = new Notification(title, { body: body.slice(0, 120), tag: "conv" + convId });
  n.onclick = () => { window.focus(); if (convId) openConv(convId); n.close(); };
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

  $("btnSettings").onclick = openSettings;

  $("stLang").onchange = () => {
    S.lang = $("stLang").value;
    localStorage.setItem("lang", S.lang);
    applyI18n();
    renderLists();
    if (S.cur) { renderChatHead(); renderMsgs(false); renderBanner(); renderTyping(); }
  };

  $("stTheme").onchange = () => {
    S.theme = $("stTheme").value;
    localStorage.setItem("theme", S.theme);
    document.body.classList.toggle("light", S.theme === "light");
  };

  $("stShutdown").onclick = async () => {
    if (!confirm(t("shutdown_confirm"))) return;
    try { await api("/api/shutdown", {}); } catch (e) { /* 连接断开是预期的 */ }
    // 尝试关掉本标签页（只影响当前页，不动浏览器其他标签）；
    // 启动器开的新标签可直接关，关不掉时退化成一个提示页
    setTimeout(() => {
      window.open("", "_self");
      window.close();
      setTimeout(() => {
        document.body.innerHTML =
          `<div style="height:100vh;display:flex;align-items:center;justify-content:center;` +
          `font-size:15px;color:var(--muted)">${esc(t("server_down"))}</div>`;
      }, 200);
    }, 350);
  };

  $("aePerm").onchange = () => { $("aePermHint").textContent = t("perm_" + $("aePerm").value); };

  $("libSave").onclick = saveLibFile;
  $("libNewFile").onclick = newLibFile;
  $("libSplit").onclick = libSplitClick;
  $("libEditor").addEventListener("input", () => {
    S.libDirty = true;
    $("libStatus").textContent = t("unsaved");
  });
  $("libEditor").addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") { e.preventDefault(); saveLibFile(); }
  });

  if ("Notification" in window && Notification.permission === "default") {
    document.addEventListener("click", () => Notification.requestPermission(), { once: true });
  }

  // 登录失效提示条
  $("btnAuthLogin").onclick = async () => {
    try { await api("/api/auth/login", {}); toast(t("auth_opened")); }
    catch (e) { toast(e.message, 1); }
  };
  $("btnAuthRetry").onclick = async () => {
    try { await api("/api/auth/clear", {}); } catch (e) { toast(e.message, 1); }
  };

  // 提问浮层：收起成小徽标 / 点徽标展开
  $("askCollapse").onclick = () => { S.askCollapsed = true; renderAskOverlay(); };
  $("askBadge").onclick = () => { S.askCollapsed = false; renderAskOverlay(); };

  // 附件：📎按钮 / 拖拽 / 粘贴
  $("btnAttach").onclick = () => $("fileInput").click();
  $("fileInput").onchange = async () => {
    for (const f of $("fileInput").files) await uploadFile(f);
    $("fileInput").value = "";
  };
  const chat = $("chat");
  let dragDepth = 0;
  chat.addEventListener("dragenter", (e) => {
    if (S.spectate || !e.dataTransfer.types.includes("Files")) return;
    e.preventDefault();
    dragDepth++;
    $("dropHint").classList.remove("hidden");
  });
  chat.addEventListener("dragover", (e) => {
    if (e.dataTransfer.types.includes("Files")) e.preventDefault();
  });
  chat.addEventListener("dragleave", () => {
    if (--dragDepth <= 0) { dragDepth = 0; $("dropHint").classList.add("hidden"); }
  });
  chat.addEventListener("drop", async (e) => {
    e.preventDefault();
    dragDepth = 0;
    $("dropHint").classList.add("hidden");
    if (S.spectate) return;
    for (const f of e.dataTransfer.files) await uploadFile(f);
  });

  const input = $("input");
  input.addEventListener("paste", (e) => {
    const cd = e.clipboardData;
    if (!cd) return;
    const files = [...cd.files];
    if (files.length) {
      e.preventDefault();
      files.forEach(uploadFile);
      return;
    }
    const text = cd.getData("text");
    const limit = (S.defaults && S.defaults.paste_doc_threshold) || 1500;
    if (text && text.length > limit) {
      e.preventDefault();
      uploadPastedText(text);
    }
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      sendMsg();
    }
  });
  input.addEventListener("input", () => {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 160) + "px";
    if (S.cur) saveDraft(S.cur.id);
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
