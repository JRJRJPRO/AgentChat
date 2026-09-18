/* settings.js —— 设置弹窗（主题/语言/用量/预警滑杆/技能入口）与登录失效提示条 */
"use strict";

function fmtPollSec(v) {
  return v % 60 === 0 ? `${v / 60} ${t("minutes")}` : `${v}s`;
}

function renderUsageWarnSettings() {
  const m = S.usageMonitor || {};
  $("stWarnPct").value = m.warn_pct || 80;
  $("stPollSec").value = m.poll_secs || 180;
  $("stWarnPctV").textContent = warnPctText(+$("stWarnPct").value);
  $("stPollSecV").textContent = fmtPollSec(+$("stPollSec").value);
  const fail = $("stUsageFail");
  fail.classList.toggle("hidden", !m.failing);
  if (m.failing) fail.textContent = `⚠ ${usageFailText()}`;
}

// 阈值滑杆拉到 100 = 关闭预警
function warnPctText(v) { return v >= 100 ? t("warn_off") : `${v}%`; }

// 用量查询故障的说明文字：限流（临时，自动退避重试）与真失败（格式可能变了）分开讲
function usageFailText() {
  const m = S.usageMonitor || {};
  if (m.fail_reason === "rate_limited") {
    return t("usage_ratelimited").replace("{t}", m.retry_at ? fmtTime(m.retry_at) : "?");
  }
  return t("usage_fail_note");
}

async function saveUsageWarnSettings() {
  try {
    const r = await api("/api/usage_settings", {
      warn_pct: +$("stWarnPct").value, poll_secs: +$("stPollSec").value,
    });
    S.usageMonitor = { ...(S.usageMonitor || {}), ...r };
  } catch (e) { toast(e.message, 1); }
}

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
  renderUsageWarnSettings();
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

// ---------------- Claude 登录失效提示条 ----------------

function renderAuthBar() {
  const bar = $("authBar");
  if (!S.auth || S.authHidden) return bar.classList.add("hidden");
  $("btnAuthHide").title = t("auth_hide");
  const limit = S.auth.kind === "limit";  // 订阅用量打满：会自动恢复，重试按钮只是手动兜底
  let text = `⚠ ${t(limit ? "limit_needed" : "auth_needed")}`;
  if (limit && S.auth.resets_at) {
    const ts = new Date(S.auth.resets_at).getTime() / 1000;
    if (!isNaN(ts)) text += t("limit_auto_resume").replace("{t}", fmtTime(ts));
  }
  $("authText").textContent = text + (S.auth.agent ? `（${S.auth.agent}: ${S.auth.detail || ""}）` : "");
  $("btnAuthLogin").classList.toggle("hidden", limit);
  $("btnAuthRetry").textContent = limit ? t("retry") : t("auth_retry");
  bar.classList.remove("hidden");
}
