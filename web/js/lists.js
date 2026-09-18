/* lists.js —— 左侧栏：会话列表 / agent 列表 / tab 切换 */
"use strict";

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
  // 归档的沉底；其余按最近唤醒时间倒序（刚叫过的浮到最上面，好找），从未唤醒的按 id
  const ordered = [...S.agents].sort((x, y) =>
    (x.status === "archived" ? 1 : 0) - (y.status === "archived" ? 1 : 0) ||
    (y.last_wake_at || 0) - (x.last_wake_at || 0) || x.id - y.id);
  for (const a of ordered) {
    const card = document.createElement("div");
    card.className = "agent-card" + (a.status === "archived" ? " archived" : "");
    const running = S.working.has(a.id) || S.waiting.has(a.id);
    const stText = S.compacting.has(a.id) ? t("compacting")
      : S.probing.has(a.id) ? t("probing")
      : S.waiting.has(a.id) ? `⏳ ${t("waiting")}`
      : S.working.has(a.id) ? t("working") : t(a.status === "active" ? "online" : a.status);
    const av = a.status === "archived"
      ? avatarHtml(a.name, false, "round archived", "", a.id)
      : avatarHtml(a.name, false, "round", agentDot(a), a.id);
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
