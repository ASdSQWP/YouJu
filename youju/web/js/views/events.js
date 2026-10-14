import { api } from "../api.js";
import { fill, copyText, emptyState, fmtDate, h, icon, menu, modal, openSidebar, spinner, timeAgo, toast } from "../ui.js?v=20261015b";
import { loadConversations, loadEventCount, store } from "../state.js";
import { navigate, replacePath } from "../router.js";
import { avatar, checkBadge, citedSources, eventStatus, levelBadge, markdownEl, modeBadges, processBlock, sourcesModal, sourcesRow } from "../components.js";

const TABS = [
  ["pending", "待确认"],
  ["processing", "处理中"],
  ["approved", "已确认"],
  ["rejected", "已驳回"],
  ["", "全部"],
];

function integrationHelp() {
  const agents = store.agents.filter((a) => a.allow_events);
  const sample = agents[0];
  const curl = `curl -X POST ${location.origin}/api/v1/agents/${sample ? sample.id : "<智能体ID>"}/events \\
  -H "Authorization: Bearer <API Key>" \\
  -H "Content-Type: application/json" \\
  -d '{"title": "杆塔3 检测到安全带缺失", "level": "high", "source": "视觉检测", "data": {"findings": ["安全带缺失", "挂钩未连接"]}}'`;
  modal({
    title: "接入外部系统",
    width: 680,
    body: h(
      "div",
      { class: "form" },
      h("p", { class: "muted", style: { marginTop: 0 } }, "监控告警、视觉检测、工单系统把事件 POST 过来，对应的智能体会检索资料、生成带引用的处置建议，然后在这里等待人工确认。"),
      h("div", { class: "field-label" }, "可接收事件的智能体"),
      h("div", { class: "chip-row" }, ...(agents.length ? agents.map((a) => h("span", { class: "chip" }, a.emoji, ` ${a.name}（ID ${a.id}）`)) : [h("span", { class: "muted" }, "还没有开启事件接入的智能体，可以在智能体设置里打开。")])),
      h("div", { class: "field-label" }, "请求示例"),
      h("div", { class: "code-box" }, curl, h("button", { class: "icon-btn copy", type: "button", title: "复制", onclick: () => copyText(curl) }, icon("copy", 15))),
      h("div", { class: "hint" }, "level 可填 critical / high / medium / low，或 P1–P4；data 可以是任意 JSON。API Key 在「设置 → API 与集成」里创建。"),
    ),
    actions: [{ label: "去创建 API Key", onClick: () => navigate("/settings/integrations") }, { label: "知道了", kind: "primary" }],
  });
}

export function eventsView({ params }) {
  const st = { tab: "pending", events: [], counts: {}, selected: params.id ? Number(params.id) : null, detail: null, timer: null, samples: [] };
  const listEl = h("div", { class: "ev-items" });
  const tabsEl = h("div", { class: "ev-tabs" });
  const detailEl = h("div", { class: "ev-detail" });
  const split = h("div", { class: `events${st.selected ? " has-detail" : ""}` }, h("div", { class: "ev-list" }, tabsEl, listEl), detailEl);

  const simulateBtn = h("button", { class: "btn btn-primary", type: "button", onclick: (e) => simulateMenu(e.currentTarget) }, icon("zap", 15), "模拟推送");
  const el = h(
    "div",
    { class: "events-page" },
    h(
      "div",
      { class: "page-head events-head" },
      h("div", { class: "page-title" }, h("button", { class: "icon-btn only-mobile", type: "button", onclick: openSidebar, title: "菜单" }, icon("menu")), h("div", null, h("h1", null, "事件中心"), h("p", null, "告警和工单推送进来后，智能体按资料给出处置建议，值班人员确认后执行。"))),
      h("div", { class: "page-actions" }, h("button", { class: "btn", type: "button", onclick: integrationHelp }, icon("webhook", 15), "接入说明"), simulateBtn),
    ),
    split,
  );

  async function simulateMenu(anchor) {
    if (!st.samples.length) st.samples = (await api("/api/events/samples")).samples;
    const byTemplate = { safety: "安全巡检", itops: "运维值班", service: "客户服务" };
    const items = [];
    let last = "";
    for (const s of st.samples) {
      if (s.template !== last) {
        items.push({ header: true, label: byTemplate[s.template] || s.template });
        last = s.template;
      }
      items.push({
        icon: "zap",
        label: s.label,
        sub: s.title,
        onClick: async () => {
          try {
            const res = await api("/api/events/simulate", { method: "POST", body: { sample: s.key } });
            toast("事件已推送，智能体正在处理", "ok");
            st.tab = "";
            await loadList();
            select(res.id);
          } catch (err) {
            toast(err.message, "error");
          }
        },
      });
    }
    menu(anchor, items, { align: "right", width: 320 });
  }

  async function loadList() {
    try {
      const data = await api(`/api/events${st.tab ? `?status=${st.tab}` : ""}`);
      st.events = data.events;
      st.counts = data.counts;
      store.pendingEvents = data.counts.pending || 0;
      loadEventCount();
      renderTabs();
      renderList();
      if (!st.selected && st.events.length && window.innerWidth > 960) select(st.events[0].id, true);
      if (!st.events.length && !st.selected) renderEmptyDetail();
      schedulePoll();
    } catch (err) {
      fill(listEl, h("div", { class: "notice notice-danger" }, err.message));
    }
  }

  function schedulePoll() {
    clearTimeout(st.timer);
    const busy = st.events.some((e) => e.status === "processing") || st.detail?.status === "processing";
    if (busy) {
      st.timer = setTimeout(async () => {
        await loadList();
        if (st.selected) await loadDetail(st.selected, true);
      }, 1500);
    }
  }

  function renderTabs() {
    fill(tabsEl, 
      ...TABS.map(([key, label]) => {
        const count = key ? st.counts[key] || 0 : Object.values(st.counts).reduce((a, b) => a + b, 0);
        return h("button", { class: `ev-tab${st.tab === key ? " active" : ""}`, type: "button", onclick: () => ((st.tab = key), loadList()) }, label, count ? h("span", { class: "ev-count" }, count) : null);
      }),
    );
  }

  function renderList() {
    if (!st.events.length) {
      fill(listEl, h("div", { class: "ev-empty" }, icon("inbox", 22), h("div", null, st.tab === "pending" ? "没有待确认的事件" : "这里还没有事件"), h("div", { class: "muted small" }, "点右上角「模拟推送」体验一次完整流程")));
      return;
    }
    fill(listEl, 
      ...st.events.map((ev) =>
        h(
          "button",
          { class: `ev-item${ev.id === st.selected ? " active" : ""}`, type: "button", onclick: () => select(ev.id) },
          h("div", { class: "ev-row" }, levelBadge(ev.level), eventStatus(ev.status), h("span", { class: "muted small grow-right" }, timeAgo(ev.created_at))),
          h("div", { class: "ev-title" }, ev.title),
          h("div", { class: "ev-sub" }, h("span", null, ev.agent_emoji), h("span", null, ev.agent_name), ev.check?.verdict === "pass" ? h("span", { class: "ok-text" }, icon("shield", 12), " 已核验") : null),
        ),
      ),
    );
  }

  function select(id, silent = false) {
    st.selected = id;
    split.classList.add("has-detail");
    if (!silent) replacePath(`/events/${id}`);
    renderList();
    loadDetail(id);
  }

  function renderEmptyDetail() {
    fill(detailEl, emptyState("inbox", "选择左侧的事件查看处置建议", "也可以点「模拟推送」，用示例告警走一遍：推送 → 检索规程 → 生成建议 → 人工确认。"));
  }

  async function loadDetail(id, quiet = false) {
    if (!quiet) fill(detailEl, h("div", { class: "loading-row" }, spinner(16), "加载中…"));
    try {
      st.detail = await api(`/api/events/${id}`);
      renderDetail();
      schedulePoll();
    } catch (err) {
      fill(detailEl, emptyState("alert", "打不开这个事件", err.message));
    }
  }

  function renderDetail() {
    const ev = st.detail;
    const agent = store.agents.find((a) => a.id === ev.agent_id) || { name: ev.agent_name, emoji: ev.agent_emoji, color: ev.agent_color };
    const data = ev.payload?.data;
    const cited = citedSources(ev.report, ev.sources);
    const fields = data && typeof data === "object" && !Array.isArray(data) ? Object.entries(data) : [];
    const comment = h("textarea", { class: "textarea", rows: 2, placeholder: "处置说明（可选），例如：已通知现场负责人整改" });
    const review = async (action) => {
      try {
        st.detail = await api(`/api/events/${ev.id}/review`, { method: "POST", body: { action, comment: comment.value.trim() } });
        toast(action === "approve" ? "已确认处置" : "已驳回", "ok");
        renderDetail();
        loadList();
      } catch (err) {
        toast(err.message, "error");
      }
    };
    fill(detailEl, 
      h("button", { class: "btn btn-ghost btn-sm only-mobile back-btn", type: "button", onclick: () => (split.classList.remove("has-detail"), (st.selected = null), replacePath("/events")) }, icon("chevronLeft", 15), "返回列表"),
      h(
        "div",
        { class: "ev-head" },
        h("div", { class: "ev-row" }, levelBadge(ev.level), eventStatus(ev.status), h("span", { class: "muted small" }, `${ev.source || "未知来源"} · ${fmtDate(ev.created_at, true)}`)),
        h("h2", null, ev.title),
        h("div", { class: "ev-agent" }, avatar(agent, "sm"), h("span", null, agent.name), ev.model_label ? h("span", { class: "muted small" }, `· ${ev.model_label}`) : null, ...modeBadges(ev)),
      ),
      h(
        "section",
        { class: "card ev-card" },
        h("div", { class: "section-title" }, icon("database", 16), "事件数据"),
        fields.length
          ? h("div", { class: "kv" }, ...fields.flatMap(([k, v]) => [h("div", { class: "kv-k" }, k), h("div", { class: "kv-v" }, typeof v === "object" ? (Array.isArray(v) ? v.join("、") : JSON.stringify(v)) : String(v))]))
          : h("pre", { class: "code-box" }, JSON.stringify(data, null, 2) || "无"),
      ),
      h(
        "section",
        { class: "card ev-card" },
        h("div", { class: "section-title" }, icon("sparkles", 16), "处置建议", h("span", { class: "grow" }), checkBadge(ev.check)),
        ev.status === "processing"
          ? h("div", { class: "loading-row" }, spinner(16), "智能体正在检索资料、生成建议…")
          : ev.report
            ? (() => {
                const md = markdownEl(ev.report, ev.sources);
                md.addEventListener("click", (e) => {
                  const cite = e.target.closest(".cite");
                  if (cite) sourcesModal(cited, Number(cite.dataset.n));
                });
                return md;
              })()
            : h("div", { class: "notice notice-danger" }, ev.error || "没有生成处置建议"),
        cited.length ? sourcesRow(cited, (n) => sourcesModal(cited, n)) : null,
        ev.trace?.length ? processBlock(ev.trace) : null,
      ),
      ev.status === "pending"
        ? h(
            "section",
            { class: "card ev-card review" },
            h("div", { class: "section-title" }, icon("shield", 16), "人工确认"),
            h("p", { class: "muted small", style: { marginTop: 0 } }, "AI 只给建议，最终处置由人确认。确认或驳回都会记录处理人和时间。"),
            comment,
            h(
              "div",
              { class: "form-actions" },
              h("button", { class: "btn", type: "button", onclick: () => followUp(ev) }, icon("chat", 15), "继续追问"),
              h("div", { class: "grow" }),
              h("button", { class: "btn btn-danger", type: "button", onclick: () => review("reject") }, icon("x", 15), "驳回"),
              h("button", { class: "btn btn-primary", type: "button", onclick: () => review("approve") }, icon("check", 15), "确认处置"),
            ),
          )
        : ev.status !== "processing"
          ? h(
              "section",
              { class: "card ev-card" },
              h("div", { class: "section-title" }, icon("clock", 16), "处理记录"),
              ev.reviewed_at ? h("p", { style: { margin: 0 } }, `${ev.status === "approved" ? "已确认处置" : "已驳回"} · ${fmtDate(ev.reviewed_at, true)}`, ev.review_comment ? h("div", { class: "muted" }, ev.review_comment) : null) : h("p", { class: "muted", style: { margin: 0 } }, ev.error || "未处理"),
              h("div", { class: "form-actions" }, h("button", { class: "btn", type: "button", onclick: () => followUp(ev) }, icon("chat", 15), ev.conv_id ? "打开追问对话" : "继续追问")),
            )
          : null,
    );
  }

  async function followUp(ev) {
    try {
      const { conversation_id } = await api(`/api/events/${ev.id}/conversation`, { method: "POST" });
      await loadConversations();
      navigate(`/chat/${conversation_id}`);
    } catch (err) {
      toast(err.message, "error");
    }
  }

  renderTabs();
  if (st.selected) loadDetail(st.selected);
  else renderEmptyDetail();
  loadList();
  return { el, destroy: () => clearTimeout(st.timer) };
}
