/* md.js —— markdown 渲染
 * agent 的回复是 GitHub 风味 markdown（表格/标题/列表/引用/删除线等），这里手写一个
 * 够用的渲染器。全程"先转义再变换"防注入；行内代码和链接先抠成占位符，避免被二次加工。
 */
"use strict";

function mdInline(s) {
  const hold = [];
  const keep = (html) => "\x00" + (hold.push(html) - 1) + "\x00";
  s = esc(s);
  s = s.replace(/`([^`\n]+)`/g, (_, c) => keep(`<code>${c}</code>`));
  s = s.replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s<)]+)\)/g, (_, txt, u) => keep(`<a href="${u}" target="_blank">${txt}</a>`));
  s = s.replace(/(https?:\/\/[^\s<)]+)/g, (u) => keep(`<a href="${u}" target="_blank">${u}</a>`));
  s = s.replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>");
  s = s.replace(/(^|[\s(（「"，。；：、])\*([^*\n]+)\*(?=$|[\s)）」".,;:!?，。；：、！？])/g, "$1<i>$2</i>");
  s = s.replace(/~~([^~\n]+)~~/g, "<s>$1</s>");
  return s.replace(/\x00(\d+)\x00/g, (_, i) => hold[+i]);
}

// 块级：表格 / 标题 / 列表（含任务清单）/ 引用 / 分隔线 / 段落
function mdBlocks(text) {
  const lines = text.split("\n");
  let out = "", para = [], list = null, tbl = null, quote = null, m;
  const flushPara = () => {
    while (para.length && !para[0].trim()) para.shift();
    while (para.length && !para[para.length - 1].trim()) para.pop();
    if (para.length) out += `<p>${para.map(mdInline).join("\n")}</p>`;  // 段内换行靠气泡的 pre-wrap 显示
    para = [];
  };
  const flushList = () => { if (list) { out += list.html + (list.ord ? "</ol>" : "</ul>"); list = null; } };
  const flushTbl = () => { if (tbl) { out += `<div class="md-tbl"><table>${tbl}</table></div>`; tbl = null; } };
  const flushQuote = () => { if (quote) { out += `<blockquote>${quote.map(mdInline).join("\n")}</blockquote>`; quote = null; } };
  const flushAll = () => { flushPara(); flushList(); flushTbl(); flushQuote(); };
  for (const ln of lines) {
    const s = ln.trim();
    if (s.startsWith("|") && s.length > 1) {
      flushPara(); flushList(); flushQuote();
      const cells = s.replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
      if (cells.every((c) => /^:?-+:?$/.test(c))) continue;  // 表头分隔行
      const tag = tbl ? "td" : "th";
      if (tbl === null) tbl = "";
      tbl += "<tr>" + cells.map((c) => `<${tag}>${mdInline(c)}</${tag}>`).join("") + "</tr>";
    } else if ((m = s.match(/^(#{1,6})\s+(.*)/))) {
      flushAll();
      const lv = Math.min(m[1].length + 2, 6);  // # → h3 起步，气泡里不需要巨型标题
      out += `<h${lv}>${mdInline(m[2])}</h${lv}>`;
    } else if (/^(-{3,}|\*{3,}|_{3,})$/.test(s)) {
      flushAll();
      out += "<hr>";
    } else if (s.startsWith(">")) {
      flushPara(); flushList(); flushTbl();
      (quote = quote || []).push(s.replace(/^>\s?/, ""));
    } else if ((m = ln.match(/^(\s*)([-*+]|\d+[.)])\s+(.*)/))) {
      flushPara(); flushTbl(); flushQuote();
      const ord = /^\d/.test(m[2]), nested = m[1].length >= 2;
      let item = m[3];
      const task = item.match(/^\[([ xX])\]\s+(.*)/);
      if (task) item = (task[1] === " " ? "☐ " : "☑ ") + task[2];
      if (list && list.ord !== ord && !nested) flushList();
      if (!list) {
        const start = ord ? parseInt(m[2]) : 1;
        list = { ord, html: ord ? `<ol${start > 1 ? ` start="${start}"` : ""}>` : "<ul>" };
      }
      list.html += `<li${nested ? ' class="nest"' : ""}>${mdInline(item)}</li>`;
    } else if (!s) {
      flushAll();  // 空行断块
    } else {
      flushList(); flushTbl(); flushQuote();
      para.push(ln);
    }
  }
  flushAll();
  return out;
}

function mdRender(text) {
  const parts = String(text ?? "").split(/```/);
  let out = "";
  for (let i = 0; i < parts.length; i++) {
    if (i % 2 === 1) { // 代码块内部
      const lang = (parts[i].match(/^([\w+#-]*)\n/) || [])[1];
      out += "<pre>" + (lang ? `<span class="code-lang">${esc(lang)}</span>` : "") +
        "<code>" + esc(parts[i].replace(/^[\w+#-]*\n/, "").replace(/\n$/, "")) + "</code></pre>";
    } else {
      out += mdBlocks(parts[i]);
    }
  }
  return out;
}
