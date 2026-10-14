import { api } from "../api.js";
import { fill, emptyState, field, fmtMs, fmtNum, h, icon, modal, pageShell, segmented, spinner, timeAgo, toast } from "../ui.js?v=20261015b";
import { isAdmin, store } from "../state.js";
import { navigate } from "../router.js";

function bars(daily) {
  const W = 640;
  const H = 210;
  const pad = { l: 30, r: 6, t: 14, b: 26 };
  const max = Math.max(4, ...daily.map((d) => d.answers));
  const step = (W - pad.l - pad.r) / daily.length;
  const y = (v) => pad.t + (H - pad.t - pad.b) * (1 - v / max);
  const parts = [];
  for (const v of [0, Math.round(max / 2), max]) {
    parts.push(`<line x1="${pad.l}" x2="${W - pad.r}" y1="${y(v)}" y2="${y(v)}" class="grid-line"/>`, `<text x="${pad.l - 8}" y="${y(v) + 4}" class="axis" text-anchor="end">${v}</text>`);
  }
  daily.forEach((d, i) => {
    const x = pad.l + i * step + step * 0.2;
    const w = step * 0.6;
    parts.push(`<rect x="${x}" y="${y(d.answers)}" width="${w}" height="${Math.max(0, y(0) - y(d.answers))}" rx="3" class="bar-all"><title>${d.date}：回答 ${d.answers}，拒答 ${d.refused}</title></rect>`);
    if (d.refused) parts.push(`<rect x="${x}" y="${y(d.refused)}" width="${w}" height="${Math.max(0, y(0) - y(d.refused))}" rx="3" class="bar-refused"/>`);
    if (daily.length <= 14 || i % 2 === 0) parts.push(`<text x="${x + w / 2}" y="${H - 8}" class="axis" text-anchor="middle">${d.date.slice(5)}</text>`);
  });
  return h("div", { class: "bars", html: `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="每日回答量">${parts.join("")}</svg>` });
}

function kpi(label, value, sub, ic) {
  return h("div", { class: "card kpi" }, h("div", { class: "kpi-label" }, icon(ic, 14), label), h("div", { class: "kpi-value" }, value), h("div", { class: "kpi-sub" }, sub));
}

function pct(v) {
  return `${Math.round((v || 0) * 100)}%`;
}

function toEvalModal(row) {
  const kbs = store.kbs.filter((kb) => row.kb_ids.includes(kb.id));
  if (!kbs.length) {
    toast("这个智能体没有绑定知识库", "error");
    return;
  }
  const kbSelect = h("select", { class: "select" }, ...kbs.map((kb) => h("option", { value: kb.id }, kb.name)));
  const docSelect = h("select", { class: "select" });
  const loadDocs = async () => {
    const { documents } = await api(`/api/kbs/${kbSelect.value}/documents`);
    fill(docSelect, ...documents.filter((d) => d.status === "ready").map((d) => h("option", { value: d.filename }, d.filename)));
  };
  kbSelect.addEventListener("change", loadDocs);
  loadDocs();
  modal({
    title: "加入检索评测集",
    body: h("div", { class: "form" }, h("div", { class: "quote" }, row.question), field("知识库", kbSelect), field("这个问题应该命中的文档", docSelect, "之后每次调整切片或模型，都可以用评测集确认这个问题有没有被修好。")),
    actions: [
      { label: "取消" },
      {
        label: "加入",
        kind: "primary",
        onClick: async () => {
          if (!docSelect.value) throw new Error("请选择文档");
          await api(`/api/messages/${row.message_id}/to-eval`, { method: "POST", body: { kb_id: Number(kbSelect.value), expected: docSelect.value } });
          toast("已加入评测集", "ok");
        },
      },
    ],
  });
}

export function dashboardView() {
  if (!isAdmin()) return { el: pageShell({ title: "运营看板" }, emptyState("lock", "需要管理员权限", "运营看板只对管理员开放。")) };
  const body = h("div", null, h("div", { class: "loading-row" }, spinner(16), "统计中…"));
  let days = 14;
  const load = async () => {
    try {
      render(await api(`/api/stats?days=${days}`));
    } catch (err) {
      fill(body, emptyState("alert", "加载失败", err.message));
    }
  };
  const render = (d) => {
    const s = d.summary;
    const judged = (d.checks.pass || 0) + (d.checks.warn || 0) + (d.checks.fail || 0);
    fill(body, 
      h(
        "div",
        { class: "kpis" },
        kpi("回答次数", fmtNum(s.answers), `近 ${d.days} 天 · 累计 ${fmtNum(d.totals.answers)}`, "chat"),
        kpi("拒答率", pct(s.refusal_rate), "没有依据时拒答的比例", "shield"),
        kpi("引用核验通过", judged ? pct(s.pass_rate) : "—", `${fmtNum(judged)} 条回答参与核验`, "check"),
        kpi("平均耗时", fmtMs(s.avg_latency_ms), "从提问到回答完成", "clock"),
        kpi("知识库", `${fmtNum(d.totals.kbs)} 个`, `${fmtNum(d.totals.documents)} 份文档 · ${fmtNum(d.totals.chunks)} 个切片`, "book"),
        kpi("智能体", `${fmtNum(d.totals.agents)} 个`, `${fmtNum(d.totals.users)} 位成员在用`, "bot"),
        kpi("用户反馈", `${d.feedback.up} / ${d.feedback.down}`, "点赞 / 点踩", "thumbUp"),
        kpi("事件", fmtNum(d.totals.events), `待确认 ${d.events.pending || 0} · 已确认 ${d.events.approved || 0}`, "inbox"),
      ),
      h(
        "div",
        { class: "dash-grid" },
        h("section", { class: "card" }, h("div", { class: "section-title" }, icon("chart", 16), "每日回答", h("span", { class: "grow" }), h("span", { class: "legend" }, h("i", { class: "lg-all" }), "回答", h("i", { class: "lg-refused" }), "拒答")), bars(d.daily)),
        h(
          "section",
          { class: "card" },
          h("div", { class: "section-title" }, icon("cpu", 16), "模型用量"),
          d.models.length
            ? h(
                "div",
                { class: "model-usage" },
                ...d.models.map((m) =>
                  h(
                    "div",
                    { class: "usage-row" },
                    h("div", { class: "usage-name" }, h("b", null, m.label), m.errors ? h("span", { class: "badge badge-danger" }, `${m.errors} 次失败`) : null),
                    h("div", { class: "usage-meta muted small" }, `${fmtNum(m.answers)} 次 · 输入 ${fmtNum(m.input)} / 输出 ${fmtNum(m.output)} tokens · 平均 ${fmtMs(m.avg_latency_ms)}`),
                    h("div", { class: "usage-bar" }, h("span", { style: { width: `${Math.max(4, (m.answers / Math.max(...d.models.map((x) => x.answers))) * 100)}%` } })),
                  ),
                ),
              )
            : h("p", { class: "muted" }, "还没有回答记录。"),
        ),
      ),
      h(
        "div",
        { class: "dash-grid even" },
        h(
          "section",
          { class: "card" },
          h("div", { class: "section-title" }, icon("search", 16), "知识缺口", h("span", { class: "muted small" }, "因为没有依据被拒答的问题")),
          d.gaps.length
            ? h(
                "div",
                { class: "list" },
                ...d.gaps.map((g) =>
                  h(
                    "div",
                    { class: "list-row" },
                    h("div", { class: "grow" }, h("div", null, g.question), h("div", { class: "muted small" }, `${g.agent} · ${timeAgo(g.last_at)}`)),
                    h("span", { class: "badge badge-warn" }, `${g.count} 次`),
                    g.kb_ids?.length ? h("button", { class: "btn btn-sm", type: "button", onclick: () => navigate(`/kbs/${g.kb_ids[0]}`) }, "补充资料") : null,
                  ),
                ),
              )
            : h("p", { class: "muted" }, "近期没有被拒答的问题。"),
        ),
        h(
          "section",
          { class: "card" },
          h("div", { class: "section-title" }, icon("thumbDown", 16), "差评回答", h("span", { class: "muted small" }, "可以直接加入评测集回归")),
          d.negative.length
            ? h(
                "div",
                { class: "list" },
                ...d.negative.map((n) =>
                  h(
                    "div",
                    { class: "list-row" },
                    h("div", { class: "grow" }, h("div", null, n.question || "（问题缺失）"), h("div", { class: "muted small" }, `${n.agent} · ${n.note || "未填写原因"} · ${timeAgo(n.created_at)}`)),
                    h("button", { class: "btn btn-sm", type: "button", onclick: () => toEvalModal(n) }, icon("flask", 14), "加入评测"),
                  ),
                ),
              )
            : h("p", { class: "muted" }, "近期没有差评。"),
        ),
      ),
    );
  };
  load();
  return {
    el: pageShell(
      {
        title: "运营看板",
        desc: "看用量、拒答和差评，找到该补的资料。",
        actions: [segmented([[7, "近 7 天"], [14, "近 14 天"], [30, "近 30 天"]], days, (v) => ((days = v), load()))],
      },
      body,
    ),
  };
}
