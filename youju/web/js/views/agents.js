import { api } from "../api.js";
import { fill, badge, confirmDialog, emptyState, field, h, icon, pageShell, segmented, switchInput, toast } from "../ui.js?v=20261015b";
import { loadAgents, loadKbs, loadModels, store } from "../state.js";
import { navigate } from "../router.js";
import { avatar } from "../components.js";

const EMOJIS = ["🦺", "🛠️", "📘", "💬", "✨", "🤖", "⚡", "🏭", "🚆", "🏥", "⚖️", "📦", "🧾", "🔒", "🛰️", "🧪", "🏗️", "🚚", "🏦", "🎓", "🧭", "📊", "🔧", "🌱"];
const COLORS = ["#0f766e", "#2563eb", "#7c3aed", "#d97706", "#dc2626", "#0891b2", "#16a34a", "#475569"];
const STRICTNESS = [["loose", "宽松"], ["standard", "标准"], ["strict", "严格"]];

function agentCard(a) {
  const strategy = a.strategy === "agent" ? "工具调用" : "检索增强";
  return h(
    "article",
    { class: "card agent-card card-hover", onclick: () => navigate(`/chat?agent=${a.id}`) },
    h("div", { class: "agent-card-head" }, avatar(a, "lg"), h("div", { class: "agent-card-title" }, h("div", { class: "card-title" }, a.name), h("div", { class: "muted small" }, a.category, a.visibility === "private" ? " · 仅自己可见" : ""))),
    h("p", { class: "card-desc" }, a.description || "没有描述"),
    h(
      "div",
      { class: "card-meta" },
      a.kbs.length ? badge(`${a.kbs.length} 个知识库`, "plain") : badge("通用对话", "plain"),
      badge(strategy, "plain"),
      a.strict && a.kbs.length ? badge("严格依据", "accent") : null,
      a.allow_events ? badge("事件接入", "info") : null,
    ),
    h(
      "div",
      { class: "card-foot" },
      h("span", { class: "muted small grow" }, a.model_label || "跟随系统默认模型"),
      a.can_edit
        ? h(
            "button",
            {
              class: "btn btn-sm btn-ghost",
              type: "button",
              onclick: (e) => {
                e.stopPropagation();
                navigate(`/agents/${a.id}`);
              },
            },
            icon("edit", 14),
            "编辑",
          )
        : null,
      h("button", { class: "btn btn-sm btn-primary", type: "button" }, icon("chat", 14), "对话"),
    ),
  );
}

export function agentsView() {
  const grid = h("div", { class: "grid" });
  const render = () => {
    fill(grid, 
      ...store.agents.map(agentCard),
      h("button", { class: "card new-card", type: "button", onclick: () => navigate("/agents/new") }, icon("plus", 22), h("span", null, "创建智能体"), h("span", { class: "muted small" }, "从行业模板开始，几分钟配好")),
    );
  };
  render();
  loadAgents().then(render).catch(() => {});
  const el = pageShell(
    {
      title: "智能体",
      desc: "每个智能体有自己的知识库、模型和回答规则，团队成员都可以直接使用。",
      actions: [h("button", { class: "btn btn-primary", type: "button", onclick: () => navigate("/agents/new") }, icon("plus", 16), "创建智能体")],
    },
    grid,
  );
  return { el };
}

function templatePicker(templates) {
  return pageShell(
    { title: "选择模板", desc: "模板只预填人设、开场白和推荐问题，知识库和模型在下一步选择。", actions: [h("button", { class: "btn", type: "button", onclick: () => navigate("/agents") }, "返回")] },
    h(
      "div",
      { class: "grid" },
      ...templates.map((t) =>
        h(
          "button",
          { class: "card template-card card-hover", type: "button", onclick: () => navigate(`/agents/new?template=${t.key}`) },
          h("div", { class: "agent-card-head" }, avatar(t, "lg"), h("div", { class: "agent-card-title" }, h("div", { class: "card-title" }, t.name), h("div", { class: "muted small" }, t.category))),
          h("p", { class: "card-desc" }, t.description),
          h("div", { class: "card-meta" }, badge(t.strategy === "agent" ? "工具调用" : "检索增强", "plain"), t.allow_events ? badge("可接入事件", "info") : null),
        ),
      ),
    ),
  );
}

export function agentEditView({ params, query }) {
  const holder = h("div", { class: "page-holder" }, h("div", { class: "page" }, h("div", { class: "page-inner" }, h("div", { class: "skeleton-lg" }))));
  (async () => {
    try {
      if (!params.id && !query.template) {
        const { templates } = await api("/api/agents/templates");
        fill(holder, templatePicker(templates));
        return;
      }
      await Promise.all([loadKbs(), loadModels()]);
      let data;
      if (params.id) {
        data = await api(`/api/agents/${params.id}`);
      } else {
        const { templates } = await api("/api/agents/templates");
        const t = templates.find((x) => x.key === query.template) || templates[templates.length - 1];
        const pack = t.pack_name ? store.kbs.find((kb) => kb.name === t.pack_name) : null;
        data = {
          name: t.key === "blank" ? "" : `${t.name}`,
          emoji: t.emoji,
          color: t.color,
          category: t.category,
          description: t.description === "从零开始配置。" ? "" : t.description,
          system_prompt: t.system_prompt,
          opening: t.opening,
          suggestions: [...t.suggestions],
          kb_ids: pack ? [pack.id] : [],
          model_ref: null,
          strategy: t.strategy,
          strict: t.strict,
          strictness: "standard",
          top_k: 5,
          temperature: 0.3,
          allow_events: t.allow_events,
          visibility: "team",
          template: t.key,
        };
      }
      fill(holder, editor(data, params.id ? Number(params.id) : null));
    } catch (err) {
      fill(holder, pageShell({ title: "智能体" }, emptyState("alert", "加载失败", err.message)));
    }
  })();
  return { el: holder };
}

function editor(data, id) {
  const f = structuredClone({ ...data, suggestions: [...(data.suggestions || []), "", "", "", ""].slice(0, 4) });
  delete f.kbs;
  const preview = h("div", { class: "editor-preview" });
  const nameInput = h("input", { class: "input", value: f.name, maxlength: 40, placeholder: "例如：安全巡检助手", oninput: (e) => ((f.name = e.target.value), renderPreview()) });
  const categoryInput = h("input", { class: "input", value: f.category, maxlength: 16, placeholder: "例如：工业安全", oninput: (e) => (f.category = e.target.value) });
  const descInput = h("textarea", { class: "textarea", rows: 2, maxlength: 300, placeholder: "一句话说明它能帮谁做什么", oninput: (e) => ((f.description = e.target.value), renderPreview()) });
  descInput.value = f.description || "";
  const promptInput = h("textarea", { class: "textarea mono", rows: 6, maxlength: 6000, placeholder: "写清楚它的身份、服务对象和回答风格。引用规则由系统自动追加，不用在这里写。", oninput: (e) => (f.system_prompt = e.target.value) });
  promptInput.value = f.system_prompt || "";
  const openingInput = h("textarea", { class: "textarea", rows: 2, maxlength: 500, placeholder: "新对话时显示的欢迎语", oninput: (e) => ((f.opening = e.target.value), renderPreview()) });
  openingInput.value = f.opening || "";
  const suggestionInputs = f.suggestions.map((value, i) => h("input", { class: "input", value, maxlength: 80, placeholder: `推荐问题 ${i + 1}`, oninput: (e) => ((f.suggestions[i] = e.target.value), renderPreview()) }));

  const emojiRow = h("div", { class: "emoji-grid" });
  const colorRow = h("div", { class: "color-row" });
  const renderLook = () => {
    fill(emojiRow, ...EMOJIS.map((e) => h("button", { class: `emoji-opt${f.emoji === e ? " on" : ""}`, type: "button", onclick: () => ((f.emoji = e), renderLook(), renderPreview()) }, e)));
    fill(colorRow, ...COLORS.map((c) => h("button", { class: `color-opt${f.color === c ? " on" : ""}`, type: "button", style: { background: c }, title: c, onclick: () => ((f.color = c), renderLook(), renderPreview()) })));
  };
  renderLook();

  const kbList = h(
    "div",
    { class: "check-list" },
    store.kbs.length
      ? store.kbs.map((kb) => {
          const box = h("input", {
            type: "checkbox",
            checked: f.kb_ids.includes(kb.id),
            onchange: (e) => {
              f.kb_ids = e.target.checked ? [...f.kb_ids, kb.id] : f.kb_ids.filter((x) => x !== kb.id);
              renderPreview();
            },
          });
          return h("label", { class: "check-item" }, box, h("span", { class: "check-main" }, h("b", null, kb.name), h("span", { class: "muted small" }, `${kb.doc_count} 份文档 · ${kb.chunk_count} 个切片 · ${kb.embedding_label}`)));
        })
      : h("div", { class: "muted" }, "还没有知识库。", h("a", { href: "#/kbs", class: "link" }, "去创建")),
  );

  const defaultLabel = store.models.chat.find((m) => m.ref === store.models.defaults?.default_chat_model)?.label || "离线摘录";
  const modelSelect = h("select", { class: "select", onchange: (e) => (f.model_ref = e.target.value || null) }, h("option", { value: "" }, `跟随系统默认（当前：${defaultLabel}）`));
  const groups = new Map();
  for (const m of store.models.chat) {
    const key = m.local ? "内置" : m.provider;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(m);
  }
  for (const [name, models] of groups) {
    modelSelect.append(h("optgroup", { label: name }, ...models.map((m) => h("option", { value: m.ref }, m.local ? "离线摘录（不调用模型）" : `${m.model}${m.tools ? "" : "（不支持工具调用）"}`))));
  }
  modelSelect.value = f.model_ref || "";

  const strategyBox = h("div", { class: "radio-cards" });
  const renderStrategy = () => {
    fill(strategyBox, 
      ...[
        ["rag", "检索增强", "先检索再生成。任何对话模型都能用，速度快、成本低，适合制度问答和客服。"],
        ["agent", "工具调用", "模型自己决定检索什么、检索几次，适合排查类的复杂问题。需要模型支持工具调用，不支持时自动退回检索增强。"],
      ].map(([key, title, desc]) => h("button", { class: `radio-card${f.strategy === key ? " on" : ""}`, type: "button", onclick: () => ((f.strategy = key), renderStrategy()) }, h("b", null, title), h("span", null, desc))),
    );
  };
  renderStrategy();

  const topK = h("input", { class: "input input-num", type: "number", min: 1, max: 12, value: f.top_k, oninput: (e) => (f.top_k = Number(e.target.value) || 5) });
  const tempLabel = h("span", { class: "range-value" }, Number(f.temperature).toFixed(1));
  const temp = h("input", { class: "range", type: "range", min: 0, max: 1.2, step: 0.1, value: f.temperature, oninput: (e) => ((f.temperature = Number(e.target.value)), (tempLabel.textContent = f.temperature.toFixed(1))) });

  const webhook = id
    ? h("div", { class: "code-box" }, `POST ${location.origin}/api/v1/agents/${id}/events\nAuthorization: Bearer <API Key>\n{"title": "...", "level": "high", "source": "...", "data": {...}}`)
    : h("div", { class: "hint" }, "保存后会显示这个智能体的事件接入地址。");

  function renderPreview() {
    const suggestions = f.suggestions.filter((s) => s.trim());
    fill(preview, 
      h("div", { class: "preview-label" }, "预览"),
      h(
        "div",
        { class: "preview-card" },
        h("div", { class: "avatar-xl", style: { background: `color-mix(in srgb, ${f.color} 16%, transparent)` } }, f.emoji),
        h("div", { class: "preview-name" }, f.name || "未命名智能体"),
        h("div", { class: "preview-desc" }, f.opening || f.description || "这里会显示开场白"),
        h("div", { class: "preview-kbs" }, ...f.kb_ids.map((kid) => store.kbs.find((kb) => kb.id === kid)).filter(Boolean).map((kb) => h("span", { class: "chip" }, icon("book", 13), kb.name))),
        suggestions.length ? h("div", { class: "preview-suggest" }, ...suggestions.map((s) => h("div", { class: "preview-q" }, s))) : null,
      ),
    );
  }
  renderPreview();

  async function save(goChat) {
    const body = { ...f, name: f.name.trim(), suggestions: f.suggestions.map((s) => s.trim()).filter(Boolean) };
    delete body.id;
    if (!body.name) {
      toast("请填写名称", "error");
      nameInput.focus();
      return;
    }
    try {
      const saved = id ? await api(`/api/agents/${id}`, { method: "PUT", body: pick(body) }) : await api("/api/agents", { method: "POST", body: pick(body) });
      await loadAgents();
      toast("已保存", "ok");
      navigate(goChat ? `/chat?agent=${saved.id}` : "/agents");
    } catch (err) {
      toast(err.message, "error");
    }
  }

  async function remove() {
    const ok = await confirmDialog({ title: "删除智能体", message: `删除「${f.name}」后，相关对话会保留但不能继续提问，事件记录会一起删除。`, confirmText: "删除", danger: true });
    if (!ok) return;
    await api(`/api/agents/${id}`, { method: "DELETE" });
    await loadAgents();
    toast("已删除", "ok");
    navigate("/agents");
  }

  const form = h(
    "div",
    { class: "editor-form" },
    section("基本信息", "名称和外观会显示在智能体列表和对话页。", h("div", { class: "look" }, field("图标", emojiRow), field("颜色", colorRow)), h("div", { class: "row-2" }, field("名称", nameInput), field("分类", categoryInput)), field("描述", descInput)),
    section("角色设定", "引用标注和拒答规则由系统统一追加，这里只写身份和风格。", field("人设与要求", promptInput), field("开场白", openingInput), field("推荐问题", h("div", { class: "row-2" }, ...suggestionInputs))),
    section("知识与模型", "可以绑定多个知识库，检索时会合并排序。", field("知识库", kbList), field("对话模型", modelSelect, "对话页里用户也可以临时切换模型。"), field("回答策略", strategyBox)),
    section(
      "回答规则",
      "「有据」的核心：依据不足时宁可不答。",
      switchInput(f.strict, (v) => (f.strict = v), "严格依据：检索不到足够依据时直接拒答，不让模型凭常识发挥"),
      h("div", { class: "row-3" }, field("依据阈值", segmented(STRICTNESS, f.strictness, (v) => (f.strictness = v)), "越严格越容易拒答"), field("每次检索条数", topK), field(h("span", null, "温度 ", tempLabel), temp)),
    ),
    section("接入与权限", "", switchInput(f.allow_events, (v) => (f.allow_events = v), "允许外部系统推送事件（告警、工单），自动生成处置建议"), webhook, field("可见范围", segmented([["team", "团队可用"], ["private", "仅自己"]], f.visibility, (v) => (f.visibility = v)))),
  );

  return h(
    "div",
    { class: "page" },
    h(
      "div",
      { class: "page-inner" },
      h(
        "div",
        { class: "page-head" },
        h("div", { class: "page-title" }, h("button", { class: "icon-btn", type: "button", title: "返回", onclick: () => navigate("/agents") }, icon("chevronLeft")), h("div", null, h("h1", null, id ? "编辑智能体" : "创建智能体"), h("p", null, id ? f.name : "填好下面几项就能使用"))),
        h(
          "div",
          { class: "page-actions" },
          id ? h("button", { class: "btn btn-danger", type: "button", onclick: remove }, icon("trash", 15), "删除") : null,
          h("button", { class: "btn", type: "button", onclick: () => save(false) }, "保存"),
          h("button", { class: "btn btn-primary", type: "button", onclick: () => save(true) }, "保存并对话"),
        ),
      ),
      h("div", { class: "editor" }, form, preview),
    ),
  );
}

function section(title, desc, ...children) {
  return h("section", { class: "form-section" }, h("div", { class: "form-section-head" }, h("h3", null, title), desc ? h("p", null, desc) : null), ...children);
}

function pick(body) {
  const keys = ["name", "emoji", "color", "category", "description", "system_prompt", "opening", "suggestions", "kb_ids", "model_ref", "strategy", "strict", "strictness", "top_k", "temperature", "allow_events", "visibility", "template"];
  return Object.fromEntries(keys.map((k) => [k, body[k]]));
}
