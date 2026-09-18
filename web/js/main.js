/* main.js —— 事件绑定与启动（必须最后加载） */
"use strict";

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
  $("libPromote").onclick = libPromoteClick;
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
  $("btnAuthHide").onclick = () => { S.authHidden = true; renderAuthBar(); };  // 收起横幅，状态一变会自动再出来

  // 提问浮层：收起成小徽标 / 点徽标展开
  $("askCollapse").onclick = () => { S.askCollapsed = true; renderAskOverlay(); };
  $("askBadge").onclick = () => { S.askCollapsed = false; renderAskOverlay(); };

  // 用量预警滑杆：拖动实时显示数值，松手保存
  $("stWarnPct").oninput = () => { $("stWarnPctV").textContent = warnPctText(+$("stWarnPct").value); };
  $("stPollSec").oninput = () => { $("stPollSecV").textContent = fmtPollSec(+$("stPollSec").value); };
  $("stWarnPct").onchange = $("stPollSec").onchange = saveUsageWarnSettings;

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
    if (e.key !== "Enter" || e.isComposing) return;
    // Enter 发送；Ctrl+Enter 换行（textarea 默认不响应 Ctrl+Enter，手动插入）；Shift+Enter 走浏览器默认换行
    if (e.ctrlKey) {
      e.preventDefault();
      const p = input.selectionStart, q = input.selectionEnd;
      input.value = input.value.slice(0, p) + "\n" + input.value.slice(q);
      input.selectionStart = input.selectionEnd = p + 1;
      input.dispatchEvent(new Event("input"));  // 触发高度自适应 + 草稿保存
    } else if (!e.shiftKey) {
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
  $("btnCompact").onclick = compactCurrent;
  $("btnCtx").onclick = ctxReportCurrent;
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

(async function init() {
  document.body.classList.toggle("light", S.theme === "light");
  applyI18n();
  bind();
  initAgentPop();
  await refreshLists();
  connectWS();
})();
