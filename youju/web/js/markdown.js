// 只支持对话里常见的 Markdown 语法；先转义再加标签，模型输出里的 HTML 不会被执行。
const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
export const escapeHtml = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ESC[c]);

const FENCE = /^\s*(```|~~~)\s*([\w+#.-]*)\s*$/;
const HEADING = /^(#{1,6})\s+(.*)$/;
const RULE = /^\s*([-*_])(\s*\1){2,}\s*$/;
const QUOTE = /^\s*>/;
const LIST = /^(\s*)([-*+•]|\d{1,3}[.)、])\s+(.*)$/;
const TABLE_SEP = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;

export function usedNumbers(text) {
  const out = [];
  for (const m of String(text || "").matchAll(/\[(\d{1,3})\]/g)) {
    const n = Number(m[1]);
    if (!out.includes(n)) out.push(n);
  }
  return out;
}

export function stripCitations(text) {
  return String(text || "").replace(/\[(\d{1,3})\]/g, "");
}

function inline(text, cite) {
  const codes = [];
  let s = String(text).replace(/`([^`]+)`/g, (_, code) => {
    codes.push(code);
    return `\u0000${codes.length - 1}\u0000`;
  });
  s = escapeHtml(s);
  s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, (_, label, url) => `<a href="${url}" target="_blank" rel="noopener noreferrer">${label}</a>`);
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/(^|[^\w*])\*([^*\s][^*]*?)\*(?!\w)/g, "$1<em>$2</em>");
  s = s.replace(/~~([^~]+)~~/g, "<del>$1</del>");
  if (cite) {
    s = s.replace(/\[(\d{1,3})\]/g, (all, n) => (cite.has(Number(n)) ? `<button type="button" class="cite" data-n="${n}">${n}</button>` : all));
  }
  return s.replace(/\u0000(\d+)\u0000/g, (_, k) => `<code>${escapeHtml(codes[Number(k)])}</code>`);
}

function codeBlock(code, lang) {
  return `<div class="code-block"><div class="code-head"><span>${escapeHtml(lang || "text")}</span><button type="button" class="code-copy">复制</button></div><pre><code>${escapeHtml(code)}</code></pre></div>`;
}

function isTable(lines, i) {
  return i + 1 < lines.length && lines[i].includes("|") && TABLE_SEP.test(lines[i + 1]);
}

function cells(line) {
  let s = line.trim();
  if (s.startsWith("|")) s = s.slice(1);
  if (s.endsWith("|")) s = s.slice(0, -1);
  return s.split("|").map((c) => c.trim());
}

function table(lines, start, cite) {
  const head = cells(lines[start]);
  const aligns = cells(lines[start + 1]).map((c) => (c.startsWith(":") && c.endsWith(":") ? "center" : c.endsWith(":") ? "right" : ""));
  const style = (k) => (aligns[k] ? ` style="text-align:${aligns[k]}"` : "");
  let i = start + 2;
  const rows = [];
  while (i < lines.length && lines[i].includes("|") && lines[i].trim()) {
    rows.push(cells(lines[i]));
    i += 1;
  }
  const th = head.map((c, k) => `<th${style(k)}>${inline(c, cite)}</th>`).join("");
  const body = rows.map((row) => `<tr>${head.map((_, k) => `<td${style(k)}>${inline(row[k] || "", cite)}</td>`).join("")}</tr>`).join("");
  return [`<div class="table-scroll"><table><thead><tr>${th}</tr></thead><tbody>${body}</tbody></table></div>`, i];
}

function list(lines, start, cite) {
  const items = [];
  let i = start;
  while (i < lines.length) {
    const line = lines[i];
    const m = line.match(LIST);
    if (m) {
      items.push({ indent: m[1].replace(/\t/g, "  ").length, ordered: /\d/.test(m[2]), num: parseInt(m[2], 10) || 1, text: m[3] });
      i += 1;
      continue;
    }
    if (line.trim() && /^\s{2,}\S/.test(line) && items.length) {
      items[items.length - 1].text += `\n${line.trim()}`;
      i += 1;
      continue;
    }
    break;
  }
  let pos = 0;
  const build = (indent) => {
    const first = items[pos];
    const tag = first.ordered ? "ol" : "ul";
    let html = `<${tag}${first.ordered && first.num !== 1 ? ` start="${first.num}"` : ""}>`;
    while (pos < items.length && items[pos].indent >= indent) {
      const item = items[pos];
      if (item.indent > indent) {
        html = `${html.replace(/<\/li>$/, "")}${build(item.indent)}</li>`;
        continue;
      }
      pos += 1;
      html += `<li>${item.text.split("\n").map((t) => inline(t, cite)).join("<br>")}</li>`;
    }
    return `${html}</${tag}>`;
  };
  let html = "";
  while (pos < items.length) html += build(items[pos].indent);
  return [html, i];
}

function startsBlock(lines, i) {
  const line = lines[i];
  return FENCE.test(line) || HEADING.test(line) || RULE.test(line) || QUOTE.test(line) || LIST.test(line) || isTable(lines, i);
}

export function renderMarkdown(source, { cite = null } = {}) {
  const lines = String(source || "").replace(/\r\n?/g, "\n").split("\n");
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const fence = line.match(FENCE);
    if (fence) {
      const code = [];
      i += 1;
      while (i < lines.length && !lines[i].trim().startsWith(fence[1])) {
        code.push(lines[i]);
        i += 1;
      }
      i += 1;
      out.push(codeBlock(code.join("\n"), fence[2]));
      continue;
    }
    if (!line.trim()) {
      i += 1;
      continue;
    }
    const heading = line.match(HEADING);
    if (heading) {
      const level = Math.min(6, heading[1].length + 1);
      out.push(`<h${level}>${inline(heading[2], cite)}</h${level}>`);
      i += 1;
      continue;
    }
    if (RULE.test(line)) {
      out.push("<hr>");
      i += 1;
      continue;
    }
    if (QUOTE.test(line)) {
      const quote = [];
      while (i < lines.length && QUOTE.test(lines[i])) {
        quote.push(lines[i].replace(/^\s*>\s?/, ""));
        i += 1;
      }
      out.push(`<blockquote>${renderMarkdown(quote.join("\n"), { cite })}</blockquote>`);
      continue;
    }
    if (isTable(lines, i)) {
      const [html, next] = table(lines, i, cite);
      out.push(html);
      i = next;
      continue;
    }
    if (LIST.test(line)) {
      const [html, next] = list(lines, i, cite);
      out.push(html);
      i = next;
      continue;
    }
    const para = [];
    while (i < lines.length && lines[i].trim() && !startsBlock(lines, i)) {
      para.push(lines[i].trim());
      i += 1;
    }
    out.push(`<p>${para.map((t) => inline(t, cite)).join("<br>")}</p>`);
  }
  return out.join("");
}
