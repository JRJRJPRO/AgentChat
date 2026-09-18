/* library.js —— 资源库（记忆/技能的查看与网页编辑）
 * 注：按 v4 规划该功能将在 v4.2 下线（记忆回归 CC 原生方式、技能改用 ~/.claude/skills），
 * 届时整个文件删除。 */
"use strict";

async function loadLibrary() {
  try {
    S.library = await api("/api/library");
    renderLibraryList();
  } catch (e) { toast(e.message, 1); }
}

function libPackInfo() {
  if (!S.lib || !S.library) return null;
  if (S.lib.kind.startsWith("agent:")) {
    const g = libAgentGroup(S.lib.kind);
    return g ? g.packs.find((p) => p.name === S.lib.pack) || null : null;
  }
  return (S.library[S.lib.kind] || []).find((p) => p.name === S.lib.pack) || null;
}

function libAgentGroup(kind) {
  return (S.library.agent_mems || []).find((g) => `agent:${g.agent_id}` === kind) || null;
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
  // 各 agent 的工作记忆（工作目录 memory/ 实况，含它们自建的包）——记忆的事实源
  for (const g of (S.library.agent_mems || [])) {
    const kind = `agent:${g.agent_id}`;
    root.insertAdjacentHTML("beforeend",
      `<div class="lib-sect"><span>🧠 ${esc(t("lib_agent_mem").replace("{n}", g.agent_name))}</span></div>`);
    for (const p of g.packs) {
      const item = document.createElement("div");
      item.className = "lib-item" + (S.lib && S.lib.kind === kind && S.lib.pack === p.name ? " active" : "");
      item.innerHTML = `<div class="lib-name">${esc(p.name)}</div>` +
        (p.description ? `<div class="lib-desc">${esc(p.description)}</div>` : "") +
        `<div class="lib-used">${p.files.length}${esc(t("files_n"))}</div>`;
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
  const def = kind === "skills" ? "SKILL.md" : "MEMORY.md";
  const file = info && info.files.length ? (info.files.includes(def) ? def : info.files[0]) : null;
  if (file) openLibFile(file, true);
}

function renderLibView() {
  const info = libPackInfo();
  $("libTitle").textContent = S.lib ? S.lib.pack : "";
  const g = S.lib && S.lib.kind.startsWith("agent:") ? libAgentGroup(S.lib.kind) : null;
  $("libSub").textContent = !info ? "" : g
    ? t("lib_agent_mem").replace("{n}", g.agent_name)
    : t(S.lib.kind === "memories" ? "lib_memories" : "lib_skills") + " · " +
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
  $("libPromote").classList.toggle("hidden", !(S.lib && S.lib.kind.startsWith("agent:")));
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

async function libPromoteClick() {
  if (!S.lib || !S.lib.kind.startsWith("agent:")) return;
  if (!confirm(t("promote_confirm").replace("{n}", S.lib.pack))) return;
  try {
    await api("/api/library/promote", { kind: S.lib.kind, pack: S.lib.pack });
    S.memsInfo = null;  // 勾选列表缓存失效（中央库多了一个包）
    await loadLibrary();
    renderLibraryList();
    renderLibView();
    toast(t("promote_done"));
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
