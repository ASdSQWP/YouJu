import { api } from "../api.js";
import { fill, badge, confirmDialog, emptyState, field, fmtBytes, fmtDate, fmtMs, fmtNum, h, icon, modal, pageShell, segmented, spinner, timeAgo, toast } from "../ui.js?v=20261015b";
import { loadAgents, loadKbs, store } from "../state.js";
import { navigate } from "../router.js";
import { sourceCard } from "../components.js";

const ACCEPT = ".pdf,.docx,.md,.markdown,.txt,.html,.htm,.csv,.xlsx";
const DOC_STATUS = { pending: ["排队中", "info"], processing: ["解析中", "info"], ready: ["可用", "ok"], failed: ["失败", "danger"] };

function embeddingSelect(value) {
  const select = h("select", { class: "select" });
  for (const m of store.models.embedding) select.append(h("option", { value: m.ref }, m.local ? "内置哈希向量（离线可用，语义能力弱）" : m.label));
  select.value = value || store.models.defaults?.default_embedding_model || "local:hash";
  return select;
}

function createKbModal() {
  const name = h("input", { class: "input", maxlength: 80, placeholder: "例如：设备检修规程" });
  const desc = h("textarea", { class: "textarea", rows: 2, maxlength: 500, placeholder: "这个知识库收录了哪些资料" });
  let visibility = "team";
  const embed = embeddingSelect();
  modal({
    title: "新建知识库",
    body: h(
      "div",
      { class: "form" },
      field("名称", name),
      field("描述", desc),
      field("可见范围", segmented([["team", "团队可用"], ["private", "仅自己"]], visibility, (v) => (visibility = v))),
      field("向量模型", embed, "接入嵌入模型（如 text-embedding-v4、bge-m3）后语义检索效果更好；之后也可以在设置里更换并重建索引。"),
    ),
    actions: [
      { label: "取消" },
      {
        label: "创建",
        kind: "primary",
        onClick: async () => {
          if (!name.value.trim()) throw new Error("请填写名称");
          const kb = await api("/api/kbs", { method: "POST", body: { name: name.value.trim(), description: desc.value.trim(), visibility, embedding_ref: embed.value } });
          await loadKbs();
          navigate(`/kbs/${kb.id}`);
        },
      },
    ],
  });
}

function kbCard(kb) {
  return h(
    "article",
    { class: "card kb-card card-hover", onclick: () => navigate(`/kbs/${kb.id}`) },
    h("div", { class: "kb-card-head" }, h("span", { class: "kb-icon" }, icon("database", 20)), h("div", { class: "grow" }, h("div", { class: "card-title" }, kb.name), h("div", { class: "muted small" }, kb.owner_name ? `${kb.owner_name} 创建` : "")), kb.visibility === "private" ? badge("仅自己", "plain") : badge("团队", "plain")),
    h("p", { class: "card-desc" }, kb.description || "没有描述"),
    h(
      "div",
      { class: "kb-stats" },
      h("div", null, h("b", null, fmtNum(kb.doc_count)), h("span", null, "文档")),
      h("div", null, h("b", null, fmtNum(kb.chunk_count)), h("span", null, "切片")),
      h("div", null, h("b", { class: kb.failed_count ? "text-danger" : "" }, kb.failed_count ? fmtNum(kb.failed_count) : kb.processing_count ? fmtNum(kb.processing_count) : "—"), h("span", null, kb.failed_count ? "失败" : "处理中")),
    ),
    h("div", { class: "card-foot" }, h("span", { class: `muted small grow${kb.embedding_ok ? "" : " text-danger"}` }, icon("layers", 13), " ", kb.embedding_label), h("span", { class: "muted small" }, timeAgo(kb.updated_at))),
  );
}

export function kbListView() {
  const grid = h("div", { class: "grid" });
  const render = () => {
    fill(grid, 
      ...store.kbs.map(kbCard),
      h("button", { class: "card new-card", type: "button", onclick: createKbModal }, icon("plus", 22), h("span", null, "新建知识库"), h("span", { class: "muted small" }, "PDF、Word、Markdown、Excel 都可以上传")),
    );
  };
  render();
  loadKbs().then(render).catch((err) => toast(err.message, "error"));
  return {
    el: pageShell(
      { title: "知识库", desc: "上传制度、规程、手册和 FAQ。文档会按章节切片，用向量和关键词混合检索。", actions: [h("button", { class: "btn btn-primary", type: "button", onclick: createKbModal }, icon("plus", 16), "新建知识库")] },
      grid,
    ),
  };
}

export function kbDetailView({ params, query }) {
  const kbId = Number(params.id);
  const st = { kb: null, docs: [], tab: query.tab || "docs", timer: null };
  const holder = h("div", { class: "page" }, h("div", { class: "page-inner" }, h("div", { class: "skeleton-lg" })));

  async function load() {
    try {
      const [kb, docs] = await Promise.all([api(`/api/kbs/${kbId}`), api(`/api/kbs/${kbId}/documents`)]);
      st.kb = kb;
      st.docs = docs.documents;
      render();
      if (query.doc) previewDoc(Number(query.doc));
    } catch (err) {
      fill(holder, pageShell({ title: "知识库" }, emptyState("alert", "打不开这个知识库", err.message, h("button", { class: "btn", type: "button", onclick: () => navigate("/kbs") }, "返回列表"))));
    }
  }

  async function refreshDocs() {
    const [kb, docs] = await Promise.all([api(`/api/kbs/${kbId}`), api(`/api/kbs/${kbId}/documents`)]);
    st.kb = kb;
    st.docs = docs.documents;
    if (st.tab === "docs") render();
    else renderHead();
  }

  function schedulePoll() {
    clearTimeout(st.timer);
    if (st.docs.some((d) => d.status === "pending" || d.status === "processing")) {
      st.timer = setTimeout(async () => {
        await refreshDocs().catch(() => {});
        schedulePoll();
      }, 1500);
    }
  }

  const headEl = h("div");
  const tabsEl = h("div", { class: "tabs" });
  const bodyEl = h("div");

  function renderHead() {
    const kb = st.kb;
    fill(headEl, 
      h(
        "div",
        { class: "page-head" },
        h(
          "div",
          { class: "page-title" },
          h("button", { class: "icon-btn", type: "button", title: "返回", onclick: () => navigate("/kbs") }, icon("chevronLeft")),
          h("div", null, h("h1", null, kb.name), h("p", null, kb.description || "没有描述")),
        ),
        h(
          "div",
          { class: "page-actions" },
          h("span", { class: "chip" }, icon("fileText", 14), `${kb.doc_count} 份文档`),
          h("span", { class: "chip" }, icon("layers", 14), `${kb.chunk_count} 个切片`),
          h("span", { class: `chip${kb.embedding_ok ? "" : " chip-warn"}` }, icon("database", 14), kb.embedding_label),
        ),
      ),
    );
  }

  function render() {
    renderHead();
    const tabs = [
      ["docs", "文档", "fileText"],
      ["search", "召回测试", "search"],
      ["eval", "检索评测", "flask"],
      ...(st.kb.can_edit ? [["settings", "设置", "settings"]] : []),
    ];
    fill(tabsEl, ...tabs.map(([key, label, ic]) => h("button", { class: `tab${st.tab === key ? " active" : ""}`, type: "button", onclick: () => ((st.tab = key), render()) }, icon(ic, 15), label)));
    fill(bodyEl, { docs: docsTab, search: searchTab, eval: evalTab, settings: settingsTab }[st.tab]());
    if (st.tab === "docs") schedulePoll();
  }

  function docsTab() {
    const wrap = h("div");
    if (st.kb.can_edit) wrap.append(dropzone());
    if (!st.docs.length) {
      wrap.append(emptyState("fileText", "还没有文档", st.kb.can_edit ? "把文件拖到上面的区域，或点击选择文件。" : "创建者还没有上传文档。"));
      return wrap;
    }
    const rows = st.docs.map((d) => {
      const [text, kind] = DOC_STATUS[d.status] || [d.status, ""];
      return h(
        "tr",
        null,
        h("td", null, h("div", { class: "doc-name" }, h("span", { class: "doc-ext" }, (d.ext || "").replace(".", "").toUpperCase() || "?"), h("span", { class: "ellipsis", title: d.filename }, d.filename))),
        h("td", null, h("span", { class: `badge badge-${kind}`, title: d.error || "" }, d.status === "processing" || d.status === "pending" ? spinner(11) : null, text), d.error ? h("div", { class: "doc-error" }, d.error) : null),
        h("td", { class: "num" }, d.chunk_count || "—"),
        h("td", { class: "num" }, fmtBytes(d.size)),
        h("td", { class: "muted" }, fmtDate(d.created_at, true)),
        h(
          "td",
          { class: "row-actions" },
          d.status === "ready" ? h("button", { class: "icon-btn", type: "button", title: "查看切片", onclick: () => previewDoc(d.id) }, icon("eye", 16)) : null,
          h("a", { class: "icon-btn", href: `/api/documents/${d.id}/file`, title: "下载原文件" }, icon("download", 16)),
          st.kb.can_edit && d.status === "failed" ? h("button", { class: "icon-btn", type: "button", title: "重试", onclick: () => retry(d) }, icon("refresh", 16)) : null,
          st.kb.can_edit ? h("button", { class: "icon-btn danger", type: "button", title: "删除", onclick: () => removeDoc(d) }, icon("trash", 16)) : null,
        ),
      );
    });
    wrap.append(h("div", { class: "table-wrap" }, h("table", { class: "table" }, h("thead", null, h("tr", null, h("th", null, "文件"), h("th", null, "状态"), h("th", { class: "num" }, "切片"), h("th", { class: "num" }, "大小"), h("th", null, "上传时间"), h("th", null, ""))), h("tbody", null, ...rows))));
    return wrap;
  }

  function dropzone() {
    const input = h("input", { type: "file", multiple: true, accept: ACCEPT, hidden: true, onchange: (e) => upload([...e.target.files]) });
    const zone = h(
      "div",
      { class: "dropzone", onclick: () => input.click() },
      input,
      h("div", { class: "dropzone-icon" }, icon("upload", 22)),
      h("div", { class: "dropzone-title" }, "拖拽文件到这里，或点击选择"),
      h("div", { class: "muted small" }, "支持 PDF、Word（.docx）、Markdown、TXT、HTML、CSV、Excel，单个文件不超过 500 MB。中文 GBK 编码的文本也能识别。"),
    );
    zone.addEventListener("dragover", (e) => {
      e.preventDefault();
      zone.classList.add("over");
    });
    zone.addEventListener("dragleave", () => zone.classList.remove("over"));
    zone.addEventListener("drop", (e) => {
      e.preventDefault();
      zone.classList.remove("over");
      upload([...e.dataTransfer.files]);
    });
    return zone;
  }

  async function upload(files) {
    if (!files.length) return;
    const form = new FormData();
    files.forEach((file) => form.append("files", file));
    toast(`正在上传 ${files.length} 个文件…`, "info", 1600);
    try {
      const result = await api(`/api/kbs/${kbId}/documents`, { method: "POST", form });
      if (result.created.length) toast(`已上传 ${result.created.length} 个文件，正在解析`, "ok");
      for (const s of result.skipped) toast(`${s.filename}：${s.reason}`, "error", 4200);
      await refreshDocs();
      schedulePoll();
      loadKbs().catch(() => {});
    } catch (err) {
      toast(err.message, "error");
    }
  }

  async function retry(d) {
    await api(`/api/documents/${d.id}/retry`, { method: "POST" });
    await refreshDocs();
    schedulePoll();
  }

  async function removeDoc(d) {
    const ok = await confirmDialog({ title: "删除文档", message: `删除「${d.filename}」及其切片？引用过它的历史回答不受影响。`, confirmText: "删除", danger: true });
    if (!ok) return;
    await api(`/api/documents/${d.id}`, { method: "DELETE" });
    toast("已删除", "ok");
    await refreshDocs();
    loadKbs().catch(() => {});
  }

  async function previewDoc(docId) {
    try {
      const data = await api(`/api/documents/${docId}/chunks`);
      const filter = h("input", { class: "input", placeholder: "在切片中查找" });
      const list = h("div", { class: "chunk-list" });
      const draw = () => {
        const q = filter.value.trim();
        fill(list, 
          ...data.chunks
            .filter((c) => !q || c.text.includes(q) || (c.section || "").includes(q))
            .map((c) => h("div", { class: "chunk" }, h("div", { class: "chunk-head" }, h("span", { class: "badge badge-plain" }, `#${c.ord + 1}`), c.section ? h("span", { class: "muted small" }, c.section) : null, c.page ? h("span", { class: "muted small" }, `第 ${c.page} 页`) : null, h("span", { class: "muted small grow-right" }, `${c.text.length} 字`)), h("div", { class: "chunk-text" }, c.text))),
        );
      };
      filter.addEventListener("input", draw);
      draw();
      modal({ title: data.document.filename, width: 760, body: h("div", null, h("p", { class: "muted small", style: { marginTop: 0 } }, `${data.chunks.length} 个切片 · 按章节切分，每片约 ${st.kb.chunk_size} 字，相邻切片重叠约 ${st.kb.chunk_overlap} 字`), filter, list) });
    } catch (err) {
      toast(err.message, "error");
    }
  }

  function searchTab() {
    const input = h("input", { class: "input input-lg", placeholder: "输入一个用户可能会问的问题，看看能检索到什么", value: st.lastQuery || "" });
    let topK = 5;
    let strictness = "standard";
    const out = h("div", { class: "search-out" });
    const run = async () => {
      const q = input.value.trim();
      if (!q) return;
      st.lastQuery = q;
      fill(out, h("div", { class: "loading-row" }, spinner(16), "检索中…"));
      try {
        const r = await api(`/api/kbs/${kbId}/search`, { method: "POST", body: { query: q, top_k: topK, strictness } });
        fill(out, 
          h(
            "div",
            { class: `verdict ${r.evidence ? "verdict-ok" : "verdict-no"}` },
            icon(r.evidence ? "shield" : "alert", 18),
            h("div", null, h("b", null, r.evidence ? "有依据" : "依据不足"), h("span", null, ` · ${r.reason} · ${fmtMs(r.elapsed_ms)}`), r.evidence ? null : h("div", { class: "small" }, "开启严格依据的智能体遇到这个问题会直接拒答，不会让模型凭常识回答。")),
          ),
          ...r.notes.map((n) => h("div", { class: "notice" }, icon("info", 15), n)),
          ...(r.hits.length ? r.hits.map((hit, i) => sourceCard({ ...hit, n: i + 1 })) : [emptyState("search", "没有相关片段", "换一种问法，或者检查知识库里是否有这部分内容。")]),
        );
      } catch (err) {
        fill(out, h("div", { class: "notice notice-danger" }, err.message));
      }
    };
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.isComposing) run();
    });
    const topSelect = h("select", { class: "select select-sm", onchange: (e) => (topK = Number(e.target.value)) }, ...[3, 5, 8, 10].map((n) => h("option", { value: n, selected: n === 5 }, `前 ${n} 条`)));
    return h(
      "div",
      null,
      h("p", { class: "muted", style: { marginTop: 0 } }, "召回测试和对话走同一套检索：向量召回 + BM25 → RRF 融合 → 可选重排 → 依据闸门。分数越高越相关。"),
      h("div", { class: "search-bar" }, input, topSelect, segmented([["loose", "宽松"], ["standard", "标准"], ["strict", "严格"]], strictness, (v) => (strictness = v)), h("button", { class: "btn btn-primary", type: "button", onclick: run }, icon("search", 16), "检索")),
      out,
    );
  }

  function evalTab() {
    const wrap = h("div", null, h("div", { class: "loading-row" }, spinner(16), "加载评测集…"));
    let data = null;
    let topK = 3;
    const docNames = st.docs.filter((d) => d.status === "ready").map((d) => d.filename);

    const draw = () => {
      const latest = data.latest;
      const byCase = new Map((latest?.details || []).map((d) => [d.case_id, d]));
      const m = latest?.metrics;
      const metric = (label, value, hint) => h("div", { class: "card kpi" }, h("div", { class: "kpi-label" }, label), h("div", { class: "kpi-value" }, value), h("div", { class: "kpi-sub" }, hint));
      const question = h("input", { class: "input", placeholder: "评测问题，例如：试用期多长？" });
      const expected = h("select", { class: "select" }, ...docNames.map((n) => h("option", { value: n }, n)));
      const rows = data.cases.map((c) => {
        const d = byCase.get(c.id);
        return h(
          "tr",
          null,
          h("td", null, c.question),
          h("td", { class: "muted" }, c.expected),
          h("td", null, d ? (d.rank ? h("span", { class: "badge badge-ok" }, `第 ${d.rank} 位`) : h("span", { class: "badge badge-danger" }, "未命中")) : h("span", { class: "muted" }, "—")),
          h("td", null, d ? (d.evidence ? h("span", { class: "badge badge-ok" }, "有依据") : h("span", { class: "badge badge-warn" }, "会拒答")) : h("span", { class: "muted" }, "—")),
          h("td", { class: "muted small" }, d ? d.got.join("、") : ""),
          st.kb.can_edit
            ? h(
                "td",
                { class: "row-actions" },
                h(
                  "button",
                  {
                    class: "icon-btn danger",
                    type: "button",
                    title: "删除",
                    onclick: async () => {
                      await api(`/api/eval/cases/${c.id}`, { method: "DELETE" });
                      load();
                    },
                  },
                  icon("trash", 15),
                ),
              )
            : h("td"),
        );
      });
      fill(wrap, 
        h("p", { class: "muted", style: { marginTop: 0 } }, "每道题标注期望命中的文档，跑一遍检索看它排第几。改了切片大小、向量模型或资料之后跑一次，就知道检索效果是变好还是变差。"),
        m
          ? h(
              "div",
              { class: "kpis" },
              metric(`Hit@1`, `${Math.round(m.hit_at_1 * 100)}%`, "期望文档排在第一"),
              metric(`Hit@${m.top_k}`, `${Math.round(m.hit_at_k * 100)}%`, `期望文档进入前 ${m.top_k}`),
              metric("MRR", m.mrr.toFixed(3), "平均倒数排名"),
              metric("依据判定率", `${Math.round(m.evidence_rate * 100)}%`, `平均 ${fmtMs(m.avg_ms)} / 题`),
            )
          : null,
        h(
          "div",
          { class: "toolbar" },
          h("span", { class: "muted" }, `${data.cases.length} 道评测题`, latest ? ` · 上次运行 ${timeAgo(latest.created_at)}` : ""),
          h("div", { class: "grow" }),
          h("select", { class: "select select-sm", onchange: (e) => (topK = Number(e.target.value)) }, ...[1, 3, 5].map((n) => h("option", { value: n, selected: n === topK }, `Top ${n}`))),
          h(
            "button",
            {
              class: "btn btn-primary",
              type: "button",
              disabled: !data.cases.length || undefined,
              onclick: async (e) => {
                e.currentTarget.disabled = true;
                try {
                  await api(`/api/kbs/${kbId}/eval/run`, { method: "POST", body: { top_k: topK } });
                  toast("评测完成", "ok");
                  load();
                } catch (err) {
                  toast(err.message, "error");
                }
              },
            },
            icon("play", 14),
            "运行评测",
          ),
        ),
        h("div", { class: "table-wrap" }, h("table", { class: "table" }, h("thead", null, h("tr", null, h("th", null, "问题"), h("th", null, "期望文档"), h("th", null, "命中"), h("th", null, "依据"), h("th", null, "实际检索到"), h("th", null, ""))), h("tbody", null, ...rows))),
        st.kb.can_edit && docNames.length
          ? h(
              "div",
              { class: "add-case" },
              question,
              expected,
              h(
                "button",
                {
                  class: "btn",
                  type: "button",
                  onclick: async () => {
                    if (!question.value.trim()) return;
                    await api(`/api/kbs/${kbId}/eval/cases`, { method: "POST", body: { question: question.value.trim(), expected: expected.value } });
                    load();
                  },
                },
                icon("plus", 15),
                "添加",
              ),
            )
          : null,
        data.runs.length > 1
          ? h(
              "div",
              { class: "history" },
              h("h4", null, "历史记录"),
              ...data.runs.map((r) => h("div", { class: "history-row" }, h("span", { class: "muted" }, fmtDate(r.created_at, true)), h("span", null, `Hit@${r.top_k} ${Math.round(r.metrics.hit_at_k * 100)}%`), h("span", null, `MRR ${r.metrics.mrr.toFixed(3)}`), h("span", null, `依据 ${Math.round(r.metrics.evidence_rate * 100)}%`))),
            )
          : null,
      );
    };
    const load = async () => {
      try {
        data = await api(`/api/kbs/${kbId}/eval`);
        draw();
      } catch (err) {
        fill(wrap, h("div", { class: "notice notice-danger" }, err.message));
      }
    };
    load();
    return wrap;
  }

  function settingsTab() {
    const kb = st.kb;
    const name = h("input", { class: "input", value: kb.name, maxlength: 80 });
    const desc = h("textarea", { class: "textarea", rows: 2, maxlength: 500 });
    desc.value = kb.description || "";
    let visibility = kb.visibility;
    const embed = embeddingSelect(kb.embedding_ref);
    const size = h("input", { class: "input input-num", type: "number", min: 200, max: 2000, step: 50, value: kb.chunk_size });
    const overlap = h("input", { class: "input input-num", type: "number", min: 0, max: 400, step: 10, value: kb.chunk_overlap });
    const save = async () => {
      try {
        const res = await api(`/api/kbs/${kbId}`, {
          method: "PATCH",
          body: { name: name.value.trim(), description: desc.value.trim(), visibility, embedding_ref: embed.value, chunk_size: Number(size.value), chunk_overlap: Number(overlap.value) },
        });
        toast(res.reindexing ? "已保存，正在按新设置重建索引" : "已保存", "ok");
        await Promise.all([loadKbs(), loadAgents()]);
        await refreshDocs();
        if (res.reindexing) {
          st.tab = "docs";
          render();
        }
      } catch (err) {
        toast(err.message, "error");
      }
    };
    return h(
      "div",
      { class: "settings-form" },
      h(
        "section",
        { class: "card form" },
        field("名称", name),
        field("描述", desc),
        field("可见范围", segmented([["team", "团队可用"], ["private", "仅自己"]], visibility, (v) => (visibility = v))),
        field("向量模型", embed, "更换后会用新模型重新向量化全部文档。"),
        h("div", { class: "row-2" }, field("切片大小（字）", size), field("相邻重叠（字）", overlap)),
        h("div", { class: "form-actions" }, h("button", { class: "btn btn-primary", type: "button", onclick: save }, "保存设置")),
      ),
      h(
        "section",
        { class: "card danger-zone" },
        h("div", null, h("b", null, "重建索引"), h("p", { class: "muted small" }, "重新解析、切片并向量化全部文档。")),
        h(
          "button",
          {
            class: "btn",
            type: "button",
            onclick: async () => {
              await api(`/api/kbs/${kbId}/reindex`, { method: "POST" });
              toast("已开始重建", "ok");
              await refreshDocs();
              st.tab = "docs";
              render();
            },
          },
          icon("refresh", 15),
          "重建索引",
        ),
      ),
      h(
        "section",
        { class: "card danger-zone" },
        h("div", null, h("b", { class: "text-danger" }, "删除知识库"), h("p", { class: "muted small" }, "文档、切片和评测集会一起删除，绑定它的智能体会自动解绑。")),
        h(
          "button",
          {
            class: "btn btn-danger",
            type: "button",
            onclick: async () => {
              const ok = await confirmDialog({ title: "删除知识库", message: `确定删除「${kb.name}」吗？此操作无法撤销。`, confirmText: "删除", danger: true });
              if (!ok) return;
              await api(`/api/kbs/${kbId}`, { method: "DELETE" });
              await Promise.all([loadKbs(), loadAgents()]);
              toast("已删除", "ok");
              navigate("/kbs");
            },
          },
          icon("trash", 15),
          "删除",
        ),
      ),
    );
  }

  fill(holder, h("div", { class: "page-inner" }, headEl, tabsEl, bodyEl));
  load();
  return { el: holder, destroy: () => clearTimeout(st.timer) };
}
