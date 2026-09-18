/* chat.js —— 聊天主区：打开会话、消息渲染、发送、附件、过程动态、上下文头部 */
"use strict";

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
  initReceipts(d.conv);
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
    if (m.mtype === "agent" && (S.working.has(m.mid) || S.waiting.has(m.mid))) fetchActs(m.mid);
  }
  autoProbe(d.conv);  // 私聊对方没有上下文数字时，悄悄量一次
  $("composer").classList.toggle("hidden", S.spectate);
  $("spectateBar").classList.toggle("hidden", !S.spectate);
  const input = $("input");
  input.value = localStorage.getItem("draft:" + cid) || "";
  input.style.height = "auto";
  if (input.value) input.style.height = Math.min(input.scrollHeight, 160) + "px";
  if (!S.spectate) input.focus();
}

// 长上下文会让模型变笨且变贵：私聊头部常驻显示对方 agent 的上下文长度 + 🧹压缩按钮
function ctxText(a) {
  if (!a) return "";
  if (a.ctx_tokens === -1) return `🧹 ${t("ctx_compacted")}`;  // 刚压缩完还没量出新长度
  if (!a.ctx_tokens || a.ctx_tokens < 0) return "";
  const k = Math.round(a.ctx_tokens / 1000);
  const pct = a.ctx_window ? Math.round(a.ctx_tokens / a.ctx_window * 100) : null;
  // 长度只在唤醒/压缩/📊查询时变化，时间戳告诉用户"这个数是什么时候量的"
  const at = a.ctx_at ? t("ctx_asof").replace("{t}", fmtTime(a.ctx_at)) : "";
  return `🧠 ${k}k${pct !== null ? ` (${pct}%)` : ""}${at}`;
}

function dmAgent(c) {
  if (!c || c.type !== "dm") return null;
  const m = c.members.find((x) => x.mtype === "agent");
  return m ? agentById(m.mid) : null;
}

function renderChatHead() {
  const c = S.cur;
  const ha = dmAgent(c);  // 私聊头部头像挂 data-aid，悬浮/点击弹详情卡
  $("chatHeadAv").innerHTML = c.type === "dm"
    ? (ha ? avatarHtml(ha.name, false, "small round" + (ha.status === "archived" ? " archived" : ""), agentDot(ha), ha.id)
          : avatarHtml(c.display_name, false, "small round", ""))
    : avatarHtml(c.display_name, false, "small", "");
  $("chatTitle").textContent = c.display_name;
  let sub = c.members.map((m) => (m.mtype === "user" ? t("me") : m.name)).join("、");
  const a = dmAgent(c);
  const busy = !!a && (S.compacting.has(a.id) || S.probing.has(a.id));
  if (a && !busy) {
    const ct = ctxText(a);
    if (ct) {
      const pct = a.ctx_window ? a.ctx_tokens / a.ctx_window * 100 : 0;
      sub += ` · ${ct}${pct >= 70 ? " ⚠" : ""}`;
    } else if (a.wake_count) {
      sub += ` · 🧠 ${t("ctx_fresh")}`;
    }
  }
  $("chatSub").textContent = sub;
  // 压缩/统计进行中：头部（原上下文数字的位置旁）显示渐变进度条，两个按钮先藏起来
  if (busy) {
    $("ctxProgLabel").textContent = S.compacting.has(a.id)
      ? `🧹 ${t("ctx_compacting")}` : `📊 ${t("ctx_probing")}`;
  }
  $("ctxProgress").classList.toggle("hidden", !busy);
  $("btnCompact").classList.toggle("hidden", !a || busy);
  $("btnCtx").classList.toggle("hidden", !a || busy);
}

async function compactCurrent() {
  const a = dmAgent(S.cur);
  if (!a || S.compacting.has(a.id)) return;
  try {
    await api(`/api/agents/${a.id}/compact`, {});  // 立即返回，进度走 ws 广播
    toast(t("compact_started"));
  } catch (e) { toast(e.message, 1); }
}

// ---------------- 上下文构成（📊 无头 /context，零 API 调用） ----------------

// 从 /context 输出的"Estimated usage by category"表里抠出 [{name, tok}]
function parseCtxCats(md) {
  const sec = md.split(/###\s*Estimated usage by category/i)[1];
  if (!sec) return null;
  const rows = [];
  for (const ln of sec.split("\n")) {
    const s = ln.trim();
    if (!s.startsWith("|")) { if (rows.length) break; continue; }
    const c = s.split("|").slice(1, -1).map((x) => x.trim());
    const m = c.length >= 2 && /^([\d.]+)\s*([km]?)$/i.exec(c[1]);
    if (m) rows.push({ name: c[0], tok: parseFloat(m[1]) * ({ k: 1e3, m: 1e6 }[m[2].toLowerCase()] || 1) });
  }
  return rows.length ? rows : null;
}

// 图表两条：上=占满度量表（窗口用了多少），下=已用部分的构成堆叠条+图例。
// 类目色按固定槽位顺序取（.ctxc-1..8，已过 CVD/对比度验证），配 2px 分隔与悬停提示
function ctxChartHtml(rows) {
  if (!rows) return "";
  const used = rows.filter((r) => !/^free space$/i.test(r.name));
  const total = rows.reduce((n, r) => n + r.tok, 0);
  const usedTok = used.reduce((n, r) => n + r.tok, 0);
  if (!total || !usedTok || !used.length) return "";
  let h = `<div class="ctx-meter"><i style="width:${Math.max(0.5, usedTok / total * 100).toFixed(1)}%"></i></div>` +
    `<div class="ctx-meter-lab"><span>${esc(t("ctx_used"))} ${fmtTok(usedTok)} · ${Math.round(usedTok / total * 100)}%</span>` +
    `<span>${esc(t("ctx_free"))} ${fmtTok(total - usedTok)}</span></div>`;
  const segs = used.map((r, i) => ({ ...r, cls: `ctxc-${(i % 8) + 1}`, share: r.tok / usedTok }));
  h += `<div class="ctx-stack">` + segs.map((s) =>
    `<i class="${s.cls}" style="flex-basis:${(s.share * 100).toFixed(2)}%" ` +
    `title="${esc(s.name)} · ${fmtTok(s.tok)} · ${(s.share * 100).toFixed(s.share < 0.1 ? 1 : 0)}%"></i>`).join("") + `</div>`;
  h += `<div class="ctx-legend">` + segs.map((s) =>
    `<span><i class="${s.cls}"></i>${esc(s.name)} <b>${fmtTok(s.tok)}</b> · ${(s.share * 100).toFixed(s.share < 0.1 ? 1 : 0)}%</span>`).join("") + `</div>`;
  return h;
}

async function ctxReportCurrent() {
  const a = dmAgent(S.cur);
  if (!a || S.probing.has(a.id)) return;
  $("ctxReport").innerHTML = `<p class="muted">${esc(t("ctx_probing"))}</p>`;
  openModal("modalCtx");
  try {
    const r = await api(`/api/agents/${a.id}/context`, {});
    $("ctxReport").innerHTML = ctxChartHtml(parseCtxCats(r.report)) + mdRender(r.report);
  } catch (e) {
    $("ctxReport").innerHTML = `<p class="muted">${esc(e.message)}</p>`;
  }
}

// 打开私聊时的智能补数：有数字就不动（两次唤醒之间不会变），
// 没数字（待统计/刚压缩完）且它空闲，才悄悄跑一次 /context 量出来
function autoProbe(c) {
  const a = dmAgent(c);
  if (!a || a.status !== "active" || !a.wake_count || a.ctx_tokens > 0) return;
  if (S.working.has(a.id) || S.compacting.has(a.id) || S.probing.has(a.id) || S.waiting.has(a.id)) return;
  api(`/api/agents/${a.id}/context`, {}).catch(() => {});  // 结果走 ws ctx 广播
}

// ---------------- 消息渲染 ----------------

function pushMsg(m, prepend) {
  if (S.msgIds.has(m.id)) return false;
  S.msgIds.add(m.id);
  if (prepend) S.msgs.unshift(m);
  else S.msgs.push(m);
  return true;
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
  if (m.stype === "note") return noteHtml(m);
  if (m.stype === "system") {
    return `<div class="sys-msg ${m.kind === "error" ? "error" : ""}">${esc(m.content)} · ${fmtTime(m.created_at)}</div>`;
  }
  const mine = m.stype === "user";
  const a = m.stype === "agent" ? agentById(m.sid) : null;
  const showName = !mine && S.cur.type === "group";
  return (
    `<div class="msg-row ${mine ? "mine" : ""}" data-mid="${m.id}">` +
    avatarHtml(mine ? "" : m.sender, mine, "small round", "", a ? a.id : 0) +
    `<div class="msg-body">` +
    (showName ? `<div class="msg-sender">${esc(m.sender)}</div>` : "") +
    `<div class="bubble">${mdRender(m.content)}${attsHtml(m.attachments)}</div>` +
    `<div class="msg-time">${fmtTime(m.created_at)}` +
    (mine ? `<span class="rcpt" data-rcpt="${m.id}">${esc(receiptText(m.id))}</span>` : "") +
    `</div></div></div>`
  );
}

// ✓ 已送达回执：agent 的投递游标 >= 消息 id 就算收到（进了唤醒词/工具捎带）。
// 私聊只画勾；群聊勾后跟名字，全都收到了就写"全员"
function receiptText(mid) {
  if (!S.cur) return "";
  const agents = S.cur.members.filter((m) => m.mtype === "agent");
  const got = agents.filter((m) => (S.receipts[m.mid] || 0) >= mid);
  if (!got.length) return "";
  if (S.cur.type === "dm" || agents.length === 1) return " ✓";
  if (got.length === agents.length) return ` ✓ ${t("rcpt_all")}`;
  return ` ✓ ${got.map((m) => m.name).join("、")}`;
}

function initReceipts(conv) {
  S.receipts = {};
  for (const m of conv.members) {
    if (m.mtype === "agent") S.receipts[m.mid] = m.delivered_id || 0;
  }
}

function updateReceipts() {
  document.querySelectorAll("[data-rcpt]").forEach((el) => {
    el.textContent = receiptText(+el.dataset.rcpt);
  });
}

// 观察层 note：只用户可见、永久保留在时间线里，agent 不会收到。
// kind=usage → ⚠ 系统提醒留痕；kind=act → 💭 过程卡（跑时实时滚动，跑完定格；>2 行折叠）
function noteHtml(m) {
  if (m.kind !== "act") {  // usage=⚠系统提醒留痕，compact=🧹压缩记录，remind=⏰定时提醒
    const icon = { compact: "🧹", remind: "⏰" }[m.kind] || "⚠";
    return `<div class="note-msg" data-mid="${m.id}"><div class="note-card usage">` +
      `${icon} ${esc(m.content)}<span class="note-time">${fmtTime(m.created_at)}</span></div></div>`;
  }
  let items = [];
  try { items = m.content ? JSON.parse(m.content) : []; } catch (e) { /* 老格式忽略 */ }
  const live = !m.content && (S.working.has(m.sid) || S.waiting.has(m.sid));
  if (live) items = S.acts[m.sid] || [];
  if (!items.length && !live) return "";  // 跑完了也没过程可讲：不占地方
  const open = S.noteOpen.has(m.id);
  const shown = open ? items : (live ? items.slice(-2) : items.slice(0, 2));
  const toggle = items.length > 2
    ? `<button class="note-toggle" onclick="toggleNote(${m.id})">` +
      `${open ? esc(t("note_fold")) + " ▴" : esc(t("note_unfold").replace("{n}", items.length)) + " ▾"}</button>`
    : "";
  return `<div class="note-msg" data-mid="${m.id}"${live ? ` data-live="${m.sid}"` : ""}>` +
    `<div class="note-card"><div class="note-head">💭 ${esc(m.sender)} · ` +
    `${esc(t(live ? "note_working" : "note_done"))}<span class="note-time">${fmtTime(m.created_at)}</span></div>` +
    (shown.map(actLineHtml).join("") || `<div class="act-line"><span class="a-note">…</span></div>`) +
    toggle + `</div></div>`;
}

window.toggleNote = (mid) => {
  S.noteOpen.has(mid) ? S.noteOpen.delete(mid) : S.noteOpen.add(mid);
  const m = S.msgs.find((x) => x.id === mid);
  const el = document.querySelector(`.note-msg[data-mid="${mid}"]`);
  if (m && el) el.outerHTML = noteHtml(m);
};

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
  const waiters = S.cur.members.filter((m) => m.mtype === "agent" && S.waiting.has(m.mid));
  if (workers.length || waiters.length) {
    const intBtn = (m) =>
      `<button class="mini-btn warn" data-int="${m.mid}" title="${esc(t("interrupt_hint"))}">⏹ ${esc(t("interrupt"))} ${esc(m.name)}</button>`;
    let html = "";
    if (workers.length) {
      html += `${esc(workers.map((m) => m.name).join("、"))} ${esc(t("typing"))} ` +
        workers.map(intBtn).join(" ");
    }
    if (waiters.length) {
      html += `${workers.length ? "　" : ""}⏳ ${esc(waiters.map((m) => m.name).join("、"))} ${esc(t("waiting_hint"))} ` +
        waiters.map(intBtn).join(" ");
    }
    el.innerHTML = html;
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
  // v2.1 起过程动态直接进时间线的 💭 过程卡（观察层 note），原聊天区上方的黄条退役。
  // 这里只负责刷新当前会话里正在直播的过程卡。
  $("actFeed").classList.add("hidden");
  if (!S.cur) return;
  const box = $("msgs");
  const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 120;
  let missing = false;
  for (const m of S.msgs) {
    if (m.stype !== "note" || m.kind !== "act" || m.content) continue;
    const el = document.querySelector(`.note-msg[data-mid="${m.id}"]`);
    if (el) el.outerHTML = noteHtml(m);
    // 该渲染却不在 DOM：占位卡到达时比"working"广播早，当时被画成了空节点——补一次全量重绘
    else if (noteHtml(m)) missing = true;
  }
  if (missing) renderMsgs(false);
  if (nearBottom) box.scrollTop = box.scrollHeight;
}

async function fetchActs(aid) {
  try {
    const r = await api(`/api/agents/${aid}/activity`);
    if (r.items && r.items.length) { S.acts[aid] = r.items; renderActs(); }
  } catch (e) { /* 静默 */ }
}
