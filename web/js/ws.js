/* ws.js —— 状态刷新与 WebSocket 事件分发（事件契约见 docs/ws-events.md） */
"use strict";

async function refreshLists() {
  const st = await api("/api/state");
  S.agents = st.agents;
  S.convs = st.convs;
  S.working = new Set(st.agents.filter((a) => a.run === "working").map((a) => a.id));
  S.compacting = new Set(st.agents.filter((a) => a.run === "compacting").map((a) => a.id));
  S.probing = new Set(st.agents.filter((a) => a.run === "probing").map((a) => a.id));
  S.waiting = new Set(st.agents.filter((a) => a.run === "waiting").map((a) => a.id));
  S.models = st.models;
  S.permissions = st.permissions;
  S.defaults = st.defaults;
  S.auth = st.auth || null;
  S.usageMonitor = st.usage_monitor || null;
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
      initReceipts(d.conv);
      updateReceipts();
      renderChatHead();
      renderBanner();
      $("composer").classList.toggle("hidden", S.spectate);
      $("spectateBar").classList.toggle("hidden", !S.spectate);
    } catch (e) { /* 会话可能被删 */ }
  }
  renderLists();
  renderTyping();
}

function connectWS() {
  const ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onmessage = (ev) => {
    const d = JSON.parse(ev.data);
    if (d.t === "msg") onWsMsg(d);
    else if (d.t === "msg_update") {
      // 观察层过程卡定格：替换消息内容并原位重绘
      const i = S.cur && S.cur.id === d.conv_id ? S.msgs.findIndex((x) => x.id === d.message.id) : -1;
      if (i >= 0) {
        S.msgs[i] = d.message;
        const el = document.querySelector(`.note-msg[data-mid="${d.message.id}"]`);
        if (el) el.outerHTML = msgHtml(d.message);
      }
    } else if (d.t === "agent") {
      // working ⇄ waiting 是同一轮运行的两种面貌，翻转时不能清空过程动态
      const wasBusy = S.working.has(d.id) || S.waiting.has(d.id);
      S.working.delete(d.id); S.compacting.delete(d.id); S.probing.delete(d.id); S.waiting.delete(d.id);
      if (d.run === "working") { S.working.add(d.id); if (!wasBusy) S.acts[d.id] = []; }
      else if (d.run === "waiting") S.waiting.add(d.id);
      else if (d.run === "compacting") S.compacting.add(d.id);
      else if (d.run === "probing") S.probing.add(d.id);
      const a = agentById(d.id);
      if (a) a.run = d.run;
      renderLists();
      renderTyping();
      renderActs();  // 直播中的过程卡收尾/清理
      if (S.cur) renderChatHead();  // 压缩进度条随状态出现/消失
    } else if (d.t === "ctx") {
      const a = agentById(d.id);
      if (a) { a.ctx_tokens = d.ctx; a.ctx_window = d.win || a.ctx_window; a.ctx_at = d.at || 0; }
      if (S.cur && dmAgent(S.cur) && dmAgent(S.cur).id === d.id) renderChatHead();
    } else if (d.t === "act") {
      (S.acts[d.id] = S.acts[d.id] || []).push(d.item);
      if (S.acts[d.id].length > 100) S.acts[d.id].splice(0, S.acts[d.id].length - 100);
      renderActs();
    } else if (d.t === "auth") {
      S.auth = d.needed ? d : null;
      S.authHidden = false;  // 状态变了就重新展示（用户之前点 ✕ 收起过也一样）
      renderAuthBar();
      if (!d.needed) toast(t("auth_recovered"));
    } else if (d.t === "ask") {
      addAskCard(d.req);
    } else if (d.t === "ask_done") {
      removeAskCard(d.id);
      if (d.reason === "timeout") toast(`${d.agent || ""}: ${t("ask_timeout")}`, 1);
    } else if (d.t === "usage_alert") {
      S.usageMonitor = { ...(S.usageMonitor || {}), alert: d.alert };
      if (d.alert && d.fresh) {
        const msg = t("usage_alert_toast").replace("{p}", Math.round(d.alert.pct));
        toast(msg, 1);
        notify(0, "AgentChat", msg);
      }
    } else if (d.t === "usage_fail") {
      S.usageMonitor = { ...(S.usageMonitor || {}), failing: d.failing,
        fail_reason: d.reason || null, retry_at: d.retry_at || 0 };
      if (d.failing && d.reason === "rate_limited") {
        // 官方元数据接口限流是常态（和登录无关），自动退避即可；只在 ⚙ 设置的用量区如实显示，不弹提示打扰
      } else if (d.failing) {
        toast(`⚠ ${t("usage_fail_note")}`, 1);
        notify(0, "AgentChat", t("usage_fail_note"));
      }
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
    } else if (d.t === "receipt") {
      if (S.cur && S.cur.id === d.conv_id) {
        S.receipts[d.agent_id] = d.upto;
        updateReceipts();
      }
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
  const note = d.message.stype === "note";  // 观察层：不通知、不进预览、不算未读
  const c = S.convs.find((x) => x.id === d.conv_id);
  if (c && !note && d.message.stype !== "user" && d.message.stype !== "system") {
    notify(d.conv_id, c.display_name, `${d.message.sender}: ${d.message.content}`);
  }
  if (c) {
    if (!note) {
      c.last_msg = d.message;
      c.last_ts = d.message.created_at;
      const viewing = S.cur && S.cur.id === d.conv_id && document.hasFocus();
      if (d.message.stype !== "user" && !viewing) c.unread += 1;
      S.convs.sort((a, b) => b.last_ts - a.last_ts);
    }
  } else {
    refreshLists(); // 新会话（agent 建群/私聊我）
  }
  const dc = S.discover.find((x) => x.id === d.conv_id);
  if (dc && !note) { dc.last_msg = d.message; dc.last_ts = d.message.created_at; }
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
