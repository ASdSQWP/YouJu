import { fill, h, icon, modal, softColor, spinner, fmtMs } from "./ui.js";
import { renderMarkdown, usedNumbers } from "./markdown.js";

export function avatar(item, size = "md") {
  const color = item?.color || "#0f766e";
  return h("span", { class: `avatar avatar-${size}`, style: { background: softColor(color, 0.16), color } }, item?.emoji || "✨");
}

export function markdownEl(text, sources, streaming = false) {
  const el = h("div", { class: `md${streaming ? " streaming" : ""}`, html: renderMarkdown(text || "", { cite: new Set((sources || []).map((s) => s.n)) }) });
  if (streaming) {
    let node = el.lastElementChild;
    while (node && ["UL", "OL", "LI", "BLOCKQUOTE"].includes(node.tagName) && node.lastElementChild) node = node.lastElementChild;
    (node && !["DIV", "TABLE", "PRE", "HR"].includes(node.tagName) ? node : el).append(h("span", { class: "caret" }));
  }
  return el;
}

export function modeBadges(m) {
  const out = [];
  if (m.refused) out.push(h("span", { class: "badge badge-warn" }, icon("shield", 12), "已拒答"));
  if (m.mode === "offline") out.push(h("span", { class: "badge" }, "离线摘录"));
  if (m.mode === "fallback") out.push(h("span", { class: "badge badge-warn" }, "模型故障 · 已降级"));
  if (m.mode === "agent") out.push(h("span", { class: "badge badge-info" }, icon("zap", 12), "智能体"));
  if (m.stopped) out.push(h("span", { class: "badge" }, "已停止"));
  return out;
}

const CLAIM = {
  supported: ["有依据", "ok"],
  weak: ["依据不足", "warn"],
  uncited: ["未标注来源", "warn"],
  invalid: ["编号无效", "danger"],
};

export function showCheck(check) {
  const rows = (check.claims || []).map((c) => {
    const [label, kind] = CLAIM[c.status] || ["", ""];
    return h(
      "div",
      { class: "claim" },
      h("span", { class: `badge badge-${kind}` }, label),
      h("div", { class: "claim-text" }, c.text, c.nums?.length ? h("span", { class: "claim-nums" }, c.nums.map((n) => `[${n}]`).join("")) : null),
      c.status === "supported" || c.status === "weak" ? h("span", { class: "claim-support" }, `覆盖 ${Math.round(c.support * 100)}%`) : null,
    );
  });
  modal({
    title: "逐句引用核验",
    width: 640,
    body: h(
      "div",
      null,
      h("p", { class: "muted small", style: { marginTop: 0 } }, "每句话的关键词和它引用的原文逐一比对，覆盖率低于 30% 判为依据不足；复述问题、拒答和提示语不计入。核验在本地完成，不额外调用模型。"),
      ...rows,
    ),
  });
}

export function checkBadge(check) {
  if (!check || !check.verdict || check.verdict === "none") return null;
  const text = {
    pass: ["check-pass", "shield", `引用核验通过 ${check.supported}/${check.total}`],
    warn: ["check-warn", "alert", `${check.weak + check.uncited} 句依据不足`],
    fail: ["check-fail", "alert", "引用核验未通过"],
  }[check.verdict] || ["check-warn", "alert", "需要人工核对"];
  return h("button", { class: `check-badge ${text[0]}`, type: "button", title: "查看逐句核验", onclick: () => showCheck(check) }, icon(text[1], 14), text[2]);
}

function stepIcon(status) {
  if (status === "running") return h("span", { class: "step-icon running" }, spinner(13));
  if (status === "error") return h("span", { class: "step-icon error" }, icon("x", 14));
  return h("span", { class: "step-icon done" }, icon("check", 14));
}

export function stepRow(step) {
  const query = step.query || step.arguments?.query;
  return h(
    "div",
    { class: "step" },
    stepIcon(step.status),
    h("div", { class: "step-main" }, h("div", { class: "step-title" }, step.title), query ? h("span", { class: "step-arg" }, query) : null, step.detail ? h("div", { class: "step-detail" }, step.detail) : null),
    step.status !== "running" && step.elapsed_ms !== undefined ? h("span", { class: "step-time" }, fmtMs(step.elapsed_ms)) : null,
  );
}

export function processBlock(trace, { streaming = false, open, onToggle } = {}) {
  const running = trace.find((s) => s.status === "running");
  const total = trace.reduce((sum, s) => sum + (s.elapsed_ms || 0), 0);
  const label = running ? `${running.title}…` : `执行过程 · ${trace.length} 步 · ${fmtMs(total)}`;
  const det = h(
    "details",
    { class: "process", open: (open ?? streaming) || undefined },
    h("summary", null, running ? spinner(14) : icon("route", 15), h("span", { class: "process-label" }, label), icon("chevronDown", 15, "chev")),
    h("div", { class: "process-body" }, ...trace.map(stepRow)),
  );
  det.addEventListener("toggle", () => onToggle?.(det.open));
  return det;
}

export function reasoningBlock(text, { streaming = false, open, onToggle } = {}) {
  const det = h(
    "details",
    { class: "reasoning", open: (open ?? streaming) || undefined },
    h("summary", null, icon("lightbulb", 15), h("span", null, streaming ? "正在思考…" : "已深度思考"), icon("chevronDown", 15, "chev")),
    h("div", { class: "reasoning-body" }, text),
  );
  det.addEventListener("toggle", () => onToggle?.(det.open));
  return det;
}

export function citedSources(content, sources) {
  const byNumber = new Map((sources || []).map((s) => [s.n, s]));
  return usedNumbers(content).map((n) => byNumber.get(n)).filter(Boolean);
}

export function sourcesRow(sources, onOpen) {
  return h(
    "div",
    { class: "sources" },
    h("span", { class: "sources-label" }, icon("book", 14), `参考资料 ${sources.length}`),
    ...sources.map((s) =>
      h(
        "button",
        { class: "source-chip", type: "button", title: `${s.filename}${s.section ? ` · ${s.section}` : ""}`, onclick: () => onOpen(s.n) },
        h("span", { class: "n" }, s.n),
        h("span", { class: "name" }, s.filename.replace(/\.[^.]+$/, "")),
        s.section ? h("span", { class: "sec" }, s.section) : null,
      ),
    ),
  );
}

export function sourceCard(s, active = false) {
  const sc = s.scores || {};
  const scores = [];
  if (sc.coverage !== undefined) scores.push(h("span", { class: "score" }, `关键词覆盖 ${Math.round(sc.coverage * 100)}%`));
  if (sc.dense) scores.push(h("span", { class: "score" }, `向量 ${Number(sc.dense).toFixed(3)}`));
  if (sc.bm25) scores.push(h("span", { class: "score" }, `BM25 ${Number(sc.bm25).toFixed(2)}`));
  if (sc.rerank !== undefined && sc.rerank !== null) scores.push(h("span", { class: "score" }, `重排 ${Number(sc.rerank).toFixed(3)}`));
  return h(
    "article",
    { class: `src-card${active ? " active" : ""}`, dataset: { n: String(s.n ?? "") } },
    h(
      "div",
      { class: "src-head" },
      s.n !== undefined ? h("span", { class: "src-n" }, s.n) : null,
      h("div", { class: "src-meta" }, h("div", { class: "src-file" }, icon("fileText", 14), s.filename), h("div", { class: "src-where" }, [s.kb_name, s.section, s.page ? `第 ${s.page} 页` : ""].filter(Boolean).join(" · "))),
    ),
    h("div", { class: "src-text" }, s.text),
    scores.length ? h("div", { class: "src-scores" }, ...scores) : null,
    s.kb_id ? h("a", { class: "src-link", href: `#/kbs/${s.kb_id}?doc=${s.doc_id}` }, "在知识库中查看原文", icon("arrowRight", 14)) : null,
  );
}

export function sourcesModal(sources, focus) {
  const cited = sources.filter((s) => s.n === focus);
  const rest = sources.filter((s) => s.n !== focus);
  const dialog = modal({ title: "引用来源", width: 640, body: h("div", { class: "source-list" }, ...[...cited, ...rest].map((s) => sourceCard(s, s.n === focus))) });
  return dialog;
}

const LEVELS = {
  critical: ["紧急", "danger"],
  high: ["高", "danger"],
  medium: ["中", "warn"],
  low: ["低", "info"],
  info: ["提示", ""],
  P1: ["P1", "danger"],
  P2: ["P2", "warn"],
  P3: ["P3", "info"],
  P4: ["P4", ""],
};

export function levelBadge(level) {
  const [text, kind] = LEVELS[level] || [level || "未分级", ""];
  return h("span", { class: `badge level${kind ? ` badge-${kind}` : ""}` }, text);
}

const EVENT_STATUS = {
  processing: ["处理中", "info"],
  pending: ["待确认", "warn"],
  approved: ["已确认", "ok"],
  rejected: ["已驳回", ""],
  failed: ["失败", "danger"],
};

export function eventStatus(status) {
  const [text, kind] = EVENT_STATUS[status] || [status, ""];
  return h("span", { class: `badge${kind ? ` badge-${kind}` : ""}` }, status === "processing" ? spinner(11) : null, text);
}

export const EVENT_STATUS_TEXT = Object.fromEntries(Object.entries(EVENT_STATUS).map(([k, v]) => [k, v[0]]));
