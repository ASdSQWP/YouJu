import { api } from "../api.js";
import { loadKbs } from "../state.js";
import { navigate } from "../router.js";
import { badge, emptyState, field, fill, h, icon, pageShell, toast } from "../ui.js";

const RESULT = [
  ["pass", "通过"],
  ["fail", "失败"],
  ["untested", "未测"],
];

export function faultsView({ params }) {
  const el = h("div");
  if (params.id) renderDetail(el, Number(params.id));
  else renderList(el);
  return { el };
}

function renderList(el) {
  fill(el, pageShell({ title: "排障闭环", desc: "仿真记录。不会连接摄像机、对讲终端，也不会下发变更。" }, h("div", { class: "loading-row" }, "加载中…")));
  load().then((data) => paintList(el, data)).catch((err) => fill(el, emptyState("alert", "加载失败", err.message)));
}

async function load() {
  return api("/api/faults");
}

function paintList(el, data) {
  const title = h("input", { class: "input", maxlength: "80", placeholder: "例如：仿真机房画面中断" });
  const category = h(
    "select",
    { class: "select" },
    ...Object.entries(data.categories).map(([id, label]) => h("option", { value: id }, label)),
  );
  const version = h("input", { class: "input", maxlength: "40", placeholder: "demo-1.0.0" });
  const phenomenon = h("textarea", { class: "textarea", rows: "3", placeholder: "现象，用你自己的话写" });
  const log = h("textarea", { class: "textarea", rows: "3", placeholder: "已脱敏的仿真日志，不要粘贴真实设备输出" });
  const shell = pageShell(
    {
      title: "排障闭环",
      desc: "全部标为仿真。诊断只引用你有权阅读的规程，审核不会执行命令。",
      actions: [
        h(
          "button",
          {
            class: "btn",
            type: "button",
            onclick: async () => {
              try {
                await api("/api/faults/samples", { method: "POST", body: {} });
                await loadKbs();
                toast("已导入仿真样例", "ok");
                renderList(el);
              } catch (err) {
                toast(err.message, "error");
              }
            },
          },
          "导入仿真样例",
        ),
      ],
    },
    h(
      "section",
      { class: "card form" },
      h("div", { class: "section-title" }, icon("plus", 16), "新建仿真记录"),
      field("标题", title),
      h("div", { class: "row-2" }, field("分类", category), field("版本", version)),
      field("现象", phenomenon),
      field("已脱敏日志", log, "这里的日志是文本，不是从终端拉来的。"),
      h(
        "div",
        { class: "form-actions" },
        h(
          "button",
          {
            class: "btn btn-primary",
            type: "button",
            onclick: async () => {
              try {
                const created = await api("/api/faults", {
                  method: "POST",
                  body: {
                    title: title.value.trim(),
                    category: category.value,
                    version: version.value.trim(),
                    phenomenon: phenomenon.value.trim(),
                    log_text: log.value.trim(),
                    kb_ids: [],
                  },
                });
                toast("已创建", "ok");
                navigate(`/faults/${created.id}`);
              } catch (err) {
                toast(err.message, "error");
              }
            },
          },
          "创建",
        ),
      ),
    ),
    data.cases.length
      ? h(
          "section",
          { class: "card" },
          ...data.cases.map((item) =>
            h(
              "button",
              { class: "list-row", type: "button", onclick: () => navigate(`/faults/${item.id}`) },
              h("div", null, h("b", null, item.title), h("div", { class: "muted" }, item.category_label, " · ", item.version || "未填版本")),
              badge(item.label, "warn"),
            ),
          ),
        )
      : emptyState("fileText", "还没有排障记录", "导入仿真样例，或自己写一条现象和日志。"),
  );
  fill(el, shell);
}

function renderDetail(el, id) {
  fill(el, pageShell({ title: "排障记录", desc: "加载中…" }, h("div", { class: "loading-row" }, "加载中…")));
  api(`/api/faults/${id}`)
    .then((data) => paintDetail(el, data))
    .catch((err) => fill(el, emptyState("alert", "打不开这条记录", err.message)));
}

function paintDetail(el, data) {
  const fault = data.case;
  const latestDiag = data.diagnoses[data.diagnoses.length - 1];
  const useModel = h("input", { type: "checkbox" });
  const shell = pageShell(
    {
      title: fault.title,
      desc: "仿真闭环。变更单通过只表示你在本机点了确认。",
      actions: [
        h("a", { class: "btn", href: `/api/faults/${fault.id}/export` }, icon("download", 15), "导出 Markdown"),
        h("button", { class: "btn", type: "button", onclick: () => navigate("/faults") }, "返回列表"),
      ],
    },
    h("div", { class: "notice notice-danger" }, icon("alert", 16), h("div", null, h("b", null, fault.label), "。不是真实接入，日志也不会发到设备。")),
    h(
      "section",
      { class: "card" },
      h("div", { class: "section-title" }, "系统观测"),
      h("p", null, h("b", null, "分类 "), fault.category_label, " · ", h("b", null, "版本 "), fault.version || "未填"),
      h("p", { style: { whiteSpace: "pre-wrap" } }, fault.phenomenon),
      h("pre", { class: "fault-log" }, fault.log_text || "（无日志）"),
      h(
        "div",
        { class: "form-actions" },
        h("label", { class: "switch-row" }, useModel, h("span", null, "使用已接入的对话模型写推测（没接入就仍然只做离线摘录）")),
        h(
          "button",
          {
            class: "btn btn-primary",
            type: "button",
            onclick: async () => {
              try {
                const next = await api(`/api/faults/${fault.id}/diagnose`, { method: "POST", body: { use_model: useModel.checked } });
                toast(next.diagnosis.banner ? "依据不足，已标为非结论" : "已生成诊断和待审变更单", next.diagnosis.banner ? "info" : "ok");
                paintDetail(el, next);
              } catch (err) {
                toast(err.message, "error");
              }
            },
          },
          "生成诊断",
        ),
      ),
    ),
    latestDiag ? diagnosisCard(latestDiag) : h("section", { class: "card" }, h("p", { class: "muted" }, "还没有诊断。")),
    ...data.changes.map((change) => changeCard(el, fault.id, change, data)),
    regressionCard(el, fault.id, data),
  );
  fill(el, shell);
}

function diagnosisCard(diag) {
  return h(
    "section",
    { class: "card" },
    h("div", { class: "section-title" }, `诊断 v${diag.version}`, badge(diag.mode === "model" ? "含模型推测" : "离线摘录", "info"), diag.banner ? badge("非结论", "danger") : badge("有依据摘录", "ok")),
    diag.banner ? h("div", { class: "notice notice-danger" }, icon("alert", 16), h("div", null, h("b", null, "非结论。"), diag.gate_reason || "相关度不足，候选不能当结论。")) : null,
    h("p", { class: "muted" }, "闸门：", diag.gate_reason || "—", diag.check?.verdict && diag.check.verdict !== "none" ? ` · 词面核验 ${diag.check.verdict}（不是事实核验）` : ""),
    diag.evidence?.length ? h("h3", null, "证据") : null,
    ...(diag.evidence || []).map(sourceBlock),
    diag.candidates?.length ? h("h3", null, "候选") : null,
    ...(diag.candidates || []).map(sourceBlock),
    diag.speculation ? h("div", null, h("h3", null, "推测"), h("p", { style: { whiteSpace: "pre-wrap" } }, diag.speculation)) : null,
    diag.content ? h("div", null, h("h3", null, "摘录"), h("p", { style: { whiteSpace: "pre-wrap" } }, diag.content)) : null,
  );
}

function sourceBlock(src) {
  return h(
    "div",
    { class: "fault-src" },
    h("b", null, src.redacted ? "来源已不可见" : `${src.filename || "未命名"}${src.section ? " · " + src.section : ""}`),
    src.chunk_id && !src.redacted ? h("span", { class: "muted" }, " chunk ", String(src.chunk_id)) : null,
    src.redacted ? h("p", null, "原文已按当前权限隐藏。") : h("p", null, src.text || ""),
  );
}

function changeCard(el, caseId, change, data) {
  const suggestion = h("textarea", { class: "textarea", rows: "5" }, change.suggestion);
  suggestion.value = change.suggestion;
  const reg = change.regression;
  return h(
    "section",
    { class: "card" },
    h(
      "div",
      { class: "section-title" },
      `变更单 v${change.version}`,
      badge(change.status === "pending" ? "待审" : change.status === "approved" ? "已通过" : "已驳回", change.status === "approved" ? "ok" : change.status === "rejected" ? "danger" : "warn"),
    ),
    h("p", { style: { whiteSpace: "pre-wrap" } }, change.problem),
    field("建议（AI 只能建议）", suggestion),
    h("p", null, h("b", null, "影响范围 "), change.impact),
    h("p", { style: { whiteSpace: "pre-wrap" } }, h("b", null, "恢复方案 "), change.recovery),
    h("p", null, h("b", null, "验证项")),
    h("ul", null, ...(change.checks || []).map((item) => h("li", null, item))),
    h("p", { class: "muted" }, "审核人：", change.reviewer_name || "还没有", " ", change.reviewed_at || "", " ", change.review_note || ""),
    h("p", null, badge(reg.verdict, reg.passed ? "ok" : "warn"), ` 已记录 ${reg.recorded}/${reg.total}`),
    h(
      "div",
      { class: "form-actions" },
      h(
        "button",
        {
          class: "btn",
          type: "button",
          onclick: () => saveChange(el, caseId, change.id, suggestion.value),
        },
        change.status === "pending" ? "保存修改" : "修改并重新待审",
      ),
      change.status === "pending"
        ? h(
            "button",
            { class: "btn btn-primary", type: "button", onclick: () => review(el, caseId, change.id, "approve") },
            "通过审核",
          )
        : null,
      change.status === "pending"
        ? h("button", { class: "btn", type: "button", onclick: () => review(el, caseId, change.id, "reject") }, "驳回")
        : null,
    ),
  );
}

async function saveChange(el, caseId, changeId, suggestion) {
  try {
    await api(`/api/faults/${caseId}/changes/${changeId}`, { method: "PATCH", body: { suggestion } });
    toast("已保存。若原先已通过，现在回到待审。", "ok");
    renderDetail(el, caseId);
  } catch (err) {
    toast(err.message, "error");
  }
}

async function review(el, caseId, changeId, action) {
  try {
    await api(`/api/faults/${caseId}/changes/${changeId}/review`, { method: "POST", body: { action, note: action === "approve" ? "本地确认" : "先不采纳" } });
    toast(action === "approve" ? "已记为本地通过，没有执行变更" : "已驳回", "ok");
    renderDetail(el, caseId);
  } catch (err) {
    toast(err.message, "error");
  }
}

function regressionCard(el, caseId, data) {
  const change = data.changes[data.changes.length - 1];
  if (!change) return null;
  return h(
    "section",
    { class: "card" },
    h("div", { class: "section-title" }, `回归记录 · 变更单 v${change.version}`),
    h("p", { class: "muted" }, "结果是人工录入。未记录或标成未测时，导出不会写回归通过。"),
    ...(change.checks || []).map((item) => regressionRow(el, caseId, change.id, item)),
  );
}

function regressionRow(el, caseId, changeId, item) {
  const result = h("select", { class: "select" }, ...RESULT.map(([id, label]) => h("option", { value: id }, label)));
  result.value = "untested";
  const note = h("input", { class: "input", placeholder: "备注，例如：只在本机看了摘录" });
  return h(
    "div",
    { class: "row-2", style: { marginBottom: "8px" } },
    h("div", null, item),
    h(
      "div",
      { class: "form-actions" },
      result,
      note,
      h(
        "button",
        {
          class: "btn",
          type: "button",
          onclick: async () => {
            try {
              const saved = await api(`/api/faults/${caseId}/regressions`, {
                method: "POST",
                body: { change_id: changeId, item, result: result.value, note: note.value.trim() },
              });
              toast(saved.summary.verdict, saved.summary.passed ? "ok" : "info");
              renderDetail(el, caseId);
            } catch (err) {
              toast(err.message, "error");
            }
          },
        },
        "记下",
      ),
    ),
  );
}
