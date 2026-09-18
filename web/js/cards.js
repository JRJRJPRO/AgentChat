/* cards.js —— 越权授权卡（右下角）与 agent 提问选择卡（中央浮层） */
"use strict";

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
