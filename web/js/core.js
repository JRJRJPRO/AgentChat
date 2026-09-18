/* core.js —— 全局状态 S、基础工具（api/toast/esc/时间/头像/弹窗）
 *
 * 前端按域拆成多个经典脚本（非 ES module，共享全局作用域：内联 handler 和
 * 跨文件调用都不用改）。加载顺序见 index.html：core 最先、main 最后。
 * 状态都放在 S 里；服务器通过 WebSocket 推事件（契约见 docs/ws-events.md）。
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
  compacting: new Set(), // 正在压缩上下文的 agent id（可多个同时压）
  probing: new Set(),  // 正在查上下文构成（/context 探测，约 3-5 秒）的 agent id
  waiting: new Set(),  // ⏳ 用 Claude 自带方式等后台任务/长 sleep 的 agent id（进程活着但在等）
  defaults: {},
  models: [],
  permissions: [],
  acts: {},           // agent_id -> 本轮过程动态（思考/工具调用，只展示不进记录）
  noteOpen: new Set(),// 已展开的观察层过程卡（note 消息 id）
  pendAtts: [],       // 待发送附件 [{kind,name,path,url,size}]
  auth: null,         // 登录失效信息（null = 正常）
  receipts: {},       // 当前会话里 agent_id -> 已送达的消息 id（✓ 回执）
};

const $ = (id) => document.getElementById(id);
const t = (k) => (I18N[S.lang] && I18N[S.lang][k]) || k;

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
  if (n >= 1e6) return (n / 1e6).toFixed(1) + "m";
  if (n >= 1e3) return (n >= 1e4 ? Math.round(n / 1e3) : (n / 1e3).toFixed(1)) + "k";
  return String(Math.round(n));
}

function fmtSize(n) {
  if (n >= 1048576) return (n / 1048576).toFixed(1) + "MB";
  if (n >= 1024) return Math.round(n / 1024) + "KB";
  return n + "B";
}

const AVATAR_COLORS = ["#4f6df5","#9256d9","#c94f7c","#c96b3b","#3f9e6e","#3a8fb7","#b3822e","#6d7f3a"];
function avatarColor(name) {
  let h = 0;
  for (const ch of name) h = (h * 31 + ch.codePointAt(0)) >>> 0;
  return AVATAR_COLORS[h % AVATAR_COLORS.length];
}
function avatarHtml(name, isUser, extraCls, dotCls, aid) {
  const color = isUser ? "var(--bubble-me)" : avatarColor(name || "?");
  const ch = isUser ? t("me") : (name || "?").slice(0, 1);
  const dot = dotCls ? `<span class="dot ${dotCls}"></span>` : "";
  // aid 存在 = 这是某个 agent 的头像，挂 data-aid 让悬浮/点击弹详情卡
  const tag = aid ? ` data-aid="${aid}" class="avatar pop ${extraCls || ""}"` : ` class="avatar ${extraCls || ""}"`;
  return `<div${tag} style="background:${color}">${esc(ch)}${dot}</div>`;
}

function agentById(id) { return S.agents.find((a) => a.id === id); }
function agentDot(a) {
  if (!a) return "";
  if (S.waiting.has(a.id)) return "waiting";
  if (S.working.has(a.id) || S.compacting.has(a.id) || S.probing.has(a.id) || a.run === "working") return "working";
  if (a.status === "active") return "online";
  return "paused";
}

function applyI18n() {
  document.querySelectorAll("[data-i18n]").forEach((el) => (el.textContent = t(el.dataset.i18n)));
  document.querySelectorAll("[data-i18n-ph]").forEach((el) => (el.placeholder = t(el.dataset.i18nPh)));
  document.documentElement.lang = S.lang;
}

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

// 桌面通知（窗口不在前台时弹）
function notify(convId, title, body) {
  if (document.hasFocus() || !("Notification" in window)) return;
  if (Notification.permission === "default") { Notification.requestPermission(); return; }
  if (Notification.permission !== "granted") return;
  const n = new Notification(title, { body: body.slice(0, 120), tag: "conv" + convId });
  n.onclick = () => { window.focus(); if (convId) openConv(convId); n.close(); };
}
