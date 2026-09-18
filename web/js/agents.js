/* agents.js —— agent 详情悬浮卡、新建/编辑弹窗、群聊创建与会话信息弹窗 */
"use strict";

// ---------------- agent 详情悬浮卡（头像悬浮/点击弹出） ----------------

let popAid = 0, popPinned = false, popTimer = null;

function agentRunText(a) {
  return S.compacting.has(a.id) ? t("compacting")
    : S.probing.has(a.id) ? t("probing")
    : S.waiting.has(a.id) ? `⏳ ${t("waiting")}`
    : S.working.has(a.id) ? t("working")
    : t(a.status === "active" ? "online" : a.status);
}

function agentPopHtml(a) {
  const row = (label, val) => val ? `<div class="ap-row"><span>${esc(label)}</span><b>${esc(val)}</b></div>` : "";
  const av = avatarHtml(a.name, false, "round" + (a.status === "archived" ? " archived" : ""), agentDot(a));
  const last = a.last_wake_at ? fmtTime(a.last_wake_at) : t("never");
  const ct = ctxText(a);
  return `<div class="ap-head">${av}<div class="ap-hi"><div class="ap-name">${esc(a.name)}</div>` +
    `<div class="ap-st">${esc(agentRunText(a))}</div></div></div>` +
    `<div class="ap-body">` +
    row(t("f_model"), a.model) +
    row(t("f_perm"), t("perm_name_" + a.permission)) +
    row(t("last_wake"), `${last} · ${a.wake_count}${t("wakes")}`) +
    (ct ? row(t("ctx_label"), ct) : "") +
    row(t("f_email"), a.email) +
    row(t("f_memo"), a.memo) +
    `<div class="ap-cwd" title="${esc(a.cwd)}">${esc(a.cwd)}</div>` +
    `</div><div class="ap-btns">` +
    `<button class="mini-btn" data-ap="dm">${esc(t("dm"))}</button>` +
    `<button class="mini-btn" data-ap="edit">${esc(t("detail_edit"))}</button>` +
    `</div>`;
}

function showAgentPop(aid, anchor, pinned) {
  const a = agentById(aid);
  if (!a) return;
  popAid = aid; popPinned = pinned || popPinned;
  const pop = $("agentPop");
  pop.innerHTML = agentPopHtml(a);
  pop.classList.remove("hidden");
  pop.querySelector('[data-ap="edit"]').onclick = () => { hideAgentPop(true); openAgentEdit(a); };
  pop.querySelector('[data-ap="dm"]').onclick = async () => {
    hideAgentPop(true);
    const r = await api(`/api/agents/${aid}/dm`, {});
    await refreshLists(); openConv(r.conv_id); setTab("chats");
  };
  // 定位：优先贴头像右下，超出视口就翻到左侧/上方
  const r = anchor.getBoundingClientRect();
  const pw = pop.offsetWidth, ph = pop.offsetHeight, gap = 8;
  let left = r.right + gap, top = r.top;
  if (left + pw > window.innerWidth - 8) left = Math.max(8, r.left - pw - gap);
  if (top + ph > window.innerHeight - 8) top = Math.max(8, window.innerHeight - ph - 8);
  pop.style.left = left + "px";
  pop.style.top = top + "px";
}

function hideAgentPop(force) {
  if (popPinned && !force) return;
  popPinned = false; popAid = 0;
  $("agentPop").classList.add("hidden");
}

// 全局委托：悬浮/点击带 data-aid 的头像弹卡；卡片自身不触发隐藏
function initAgentPop() {
  const pop = $("agentPop");
  document.addEventListener("mouseover", (e) => {
    const av = e.target.closest("[data-aid]");
    if (av) {
      const aid = +av.dataset.aid;
      clearTimeout(popTimer);
      if (!popPinned || popAid === aid) popTimer = setTimeout(() => showAgentPop(aid, av, false), 220);
    } else if (!e.target.closest("#agentPop")) {
      clearTimeout(popTimer);
      if (!popPinned) popTimer = setTimeout(() => hideAgentPop(false), 250);
    }
  });
  pop.addEventListener("mouseenter", () => clearTimeout(popTimer));
  pop.addEventListener("mouseleave", () => { if (!popPinned) hideAgentPop(false); });
  document.addEventListener("click", (e) => {
    const av = e.target.closest("[data-aid]");
    if (av) { e.stopPropagation(); showAgentPop(+av.dataset.aid, av, true); }
    else if (!e.target.closest("#agentPop")) hideAgentPop(true);
  });
}

// ---------------- 新建 / 编辑 agent 弹窗 ----------------

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
  $("aeEmail").value = a.email || "";
  $("aeDirs").value = a.extra_dirs || "";
  $("aeAsk").checked = !!a.ask_perm;
  $("aePermHint").textContent = t("perm_" + a.permission);
  const ct = ctxText(a);
  $("aeInfo").textContent = `${t("f_cwd")}: ${a.cwd}` + (ct ? `　${t("ctx_label")}: ${ct}` : "");
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
      email: $("aeEmail").value,
      extra_dirs: $("aeDirs").value,
      ask_perm: $("aeAsk").checked,
      skills: checkedSkills("aeSkills"),
      memories: checkedSkills("aeMems"),
    });
    closeModal();
    refreshLists();
  } catch (e) { toast(e.message, 1); }
}

// ---------------- 新建群聊 / 会话信息 ----------------

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
      avatarHtml(isUser ? "" : m.name, isUser, "small round", a ? agentDot(a) : "", a ? a.id : 0) +
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
