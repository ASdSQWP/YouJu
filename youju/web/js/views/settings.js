import { api } from "../api.js";
import { fill, applyTheme, badge, confirmDialog, copyText, emptyState, field, fmtDate, h, icon, modal, openSidebar, segmented, spinner, switchInput, themePref, timeAgo, toast } from "../ui.js?v=20261016f";
import { isAdmin, loadAgents, loadModels, store } from "../state.js";
import { navigate } from "../router.js";

const TABS = [
  { key: "models", label: "模型服务", icon: "plug", admin: true },
  { key: "members", label: "成员管理", icon: "users", admin: true },
  { key: "integrations", label: "API 与集成", icon: "key" },
  { key: "profile", label: "个人设置", icon: "user" },
];
const REGIONS = [
  ["cn", "国内模型"],
  ["global", "海外模型"],
  ["local", "本地与自定义"],
];
const TYPE_LABEL = { chat: "对话", embedding: "向量", rerank: "重排" };

export function settingsView({ params }) {
  const tabs = TABS.filter((t) => !t.admin || isAdmin());
  const active = tabs.find((t) => t.key === params.tab)?.key || tabs[0].key;
  const content = h("div", { class: "settings-content" });
  let returnPath = "";
  try {
    returnPath = sessionStorage.getItem("youju_return") || "";
  } catch {
    returnPath = "";
  }
  const backBar = returnPath
    ? h(
        "div",
        { class: "notice", style: { marginBottom: "12px" } },
        "配置完模型后可返回视觉检测。",
        " ",
        h(
          "button",
          {
            class: "btn btn-sm btn-primary",
            type: "button",
            onclick: () => {
              try {
                sessionStorage.removeItem("youju_return");
              } catch {
                /* ignore */
              }
              navigate(returnPath);
            },
          },
          "返回视觉检测",
        ),
      )
    : null;
  const el = h(
    "div",
    { class: "page" },
    h(
      "div",
      { class: "page-inner" },
      h("div", { class: "page-head" }, h("div", { class: "page-title" }, h("button", { class: "icon-btn only-mobile", type: "button", onclick: openSidebar }, icon("menu")), h("div", null, h("h1", null, "设置"), h("p", null, "模型接入、成员、对外接口和个人偏好。")))),
      backBar,
      h(
        "div",
        { class: "settings" },
        h("nav", { class: "subnav" }, ...tabs.map((t) => h("a", { href: `#/settings/${t.key}`, class: t.key === active ? "active" : "" }, icon(t.icon, 16), t.label))),
        content,
      ),
    ),
  );
  ({ models: modelsTab, members: membersTab, integrations: integrationsTab, profile: profileTab })[active](content);
  return { el };
}

function logoBadge(name, color) {
  const text = name.replace(/[（(].*$/, "").trim();
  const short = /^[A-Za-z]/.test(text) ? text.slice(0, 2) : text.slice(0, 1);
  return h("span", { class: "prov-logo", style: { background: color } }, short);
}

async function modelsTab(root) {
  fill(root, h("div", { class: "loading-row" }, spinner(16), "加载模型服务…"));
  let data;
  let presets;
  try {
    [data, { presets }] = await Promise.all([api("/api/providers"), api("/api/providers/presets")]);
  } catch (err) {
    fill(root, emptyState("alert", "加载失败", err.message));
    return;
  }
  const reload = () => modelsTab(root);
  const presetMap = Object.fromEntries(presets.map((p) => [p.key, p]));

  const select = (type, value, allowEmpty) => {
    const s = h("select", { class: "select" });
    if (allowEmpty) s.append(h("option", { value: "" }, "不使用"));
    for (const m of store.models[type]) s.append(h("option", { value: m.ref }, m.local ? (type === "chat" ? "离线摘录（不调用模型）" : "内置哈希向量（离线）") : m.label));
    s.value = value || "";
    return s;
  };
  const chatSel = select("chat", data.defaults.default_chat_model || "local:extractive");
  const embedSel = select("embedding", data.defaults.default_embedding_model);
  const rerankSel = select("rerank", data.defaults.rerank_model, true);
  const saveDefaults = async () => {
    try {
      await api("/api/settings/models", { method: "PUT", body: { default_chat_model: chatSel.value || null, default_embedding_model: embedSel.value || null, rerank_model: rerankSel.value || null } });
      await loadModels();
      toast("默认模型已保存", "ok");
    } catch (err) {
      toast(err.message, "error");
    }
  };

  const configured = data.providers.length
    ? h(
        "div",
        { class: "card flush" },
        ...data.providers.map((p) => {
          const counts = { chat: 0, embedding: 0, rerank: 0 };
          for (const m of p.models) counts[m.type] = (counts[m.type] || 0) + 1;
          const result = h("span", { class: "test-inline" });
          return h(
            "div",
            { class: "prov-row" },
            logoBadge(p.name, p.color),
            h(
              "div",
              { class: "grow min0" },
              h("div", { class: "prov-name" }, p.name, p.enabled ? null : h("span", { class: "badge" }, "已停用")),
              h("div", { class: "prov-sub ellipsis" }, p.base_url),
              h("div", { class: "prov-sub" }, Object.entries(counts).filter(([, n]) => n).map(([k, n]) => `${TYPE_LABEL[k]} ${n}`).join(" · ") || "未配置模型", " · ", p.has_key ? `Key ${p.api_key_masked}` : "无需 Key", " ", result),
            ),
            switchInput(p.enabled, async (v) => {
              await api(`/api/providers/${p.id}`, { method: "PATCH", body: { enabled: v } });
              await loadModels();
              toast(v ? "已启用" : "已停用", "ok");
            }),
            h("button", { class: "btn btn-sm", type: "button", onclick: () => testProvider(p.id, result) }, "测试"),
            h("button", { class: "btn btn-sm btn-ghost", type: "button", onclick: () => providerModal(presetMap[p.preset] || presetMap.custom, p, reload) }, icon("edit", 14), "编辑"),
            h(
              "button",
              {
                class: "icon-btn danger",
                type: "button",
                title: "删除",
                onclick: async () => {
                  const ok = await confirmDialog({ title: "删除模型服务", message: `删除「${p.name}」？使用它的智能体会改用系统默认模型。`, confirmText: "删除", danger: true });
                  if (!ok) return;
                  try {
                    await api(`/api/providers/${p.id}`, { method: "DELETE" });
                    await Promise.all([loadModels(), loadAgents()]);
                    reload();
                  } catch (err) {
                    toast(err.message, "error");
                  }
                },
              },
              icon("trash", 15),
            ),
          );
        }),
      )
    : h("div", { class: "notice" }, icon("info", 16), "还没有接入模型。现在对话走「离线摘录」：直接摘录资料原文作答，也能完整演示检索、引用和拒答。接入任意一家后会自动设为默认模型。");

  fill(root, 
    h(
      "section",
      { class: "card form" },
      h("div", { class: "section-title" }, icon("sparkles", 16), "默认模型"),
      h("div", { class: "row-3" }, field("对话模型", chatSel, "智能体没有单独指定时使用"), field("向量模型", embedSel, "新建知识库时默认使用"), field("重排模型", rerankSel, "可选，开启后检索结果会再精排一次")),
      h("div", { class: "form-actions" }, h("button", { class: "btn btn-primary", type: "button", onclick: saveDefaults }, "保存默认模型")),
    ),
    visionModelCard(),
    h("h3", { class: "block-title" }, `已接入 ${data.providers.length}`),
    configured,
    h("h3", { class: "block-title" }, "添加模型服务"),
    ...REGIONS.map(([region, label]) =>
      h(
        "div",
        { class: "prov-group" },
        h("div", { class: "prov-group-title" }, label),
        h(
          "div",
          { class: "prov-grid" },
          ...presets
            .filter((p) => p.region === region)
            .map((p) =>
              h(
                "button",
                { class: "prov-card", type: "button", onclick: () => providerModal(p, null, reload) },
                logoBadge(p.name, p.color),
                h("div", { class: "min0" }, h("div", { class: "prov-name ellipsis" }, p.name), h("div", { class: "prov-sub" }, { openai: "OpenAI 兼容协议", anthropic: "Anthropic Messages", gemini: "Gemini 原生协议" }[p.kind], data.providers.some((x) => x.preset === p.key) ? " · 已接入" : "")),
              ),
            ),
        ),
      ),
    ),
  );
}

function visionModelCard() {
  const root = h("section", { class: "card form" }, h("div", { class: "loading-row" }, spinner(14), "加载视觉模型…"));
  api("/api/vision/model-settings")
    .then((cfg) => paintVisionModelCard(root, cfg))
    .catch((err) => fill(root, h("p", { class: "text-danger" }, err.message)));
  return root;
}

function paintVisionModelCard(root, cfg) {
  let small = cfg.small_model || "demo";
  let vlm = cfg.vlm_model || "";
  const path = h("input", { class: "input mono", value: cfg.yolo_path || "", placeholder: "D:\\models\\helmet.pt 或留空使用 data/models/*.pt" });
  const note = h("div", { class: "hint" }, "手动填写权重路径，或让已接入的大模型根据检测需求给出建议，确认后再保存。");
  const ask = h("textarea", { class: "textarea", rows: 2, placeholder: "例如：用 YOLO 检测未戴安全帽，大模型用当前的视觉模型复核" });
  const smallSel = h(
    "select",
    { class: "select", onchange: (e) => (small = e.target.value) },
    ...(cfg.small_models || []).map((m) => h("option", { value: m.id, selected: m.id === small }, m.ready === false ? `${m.name}（未就绪）` : m.name)),
  );
  const vlmSel = h(
    "select",
    { class: "select", onchange: (e) => (vlm = e.target.value) },
    h("option", { value: "", selected: !vlm }, "默认视觉模型"),
    ...(cfg.vision_models || []).map((m) => h("option", { value: m.ref, selected: m.ref === vlm }, `${m.label}${m.vision ? "" : "（未标视觉）"}`)),
  );
  fill(
    root,
    h("div", { class: "section-title" }, icon("crosshair", 16), "视觉检测模型"),
    h("p", { class: "muted", style: { marginTop: 0 } }, "小模型默认用场景演示，也可换成 YOLO。大模型从已接入的对话模型里选。这里的配置是监视墙的默认值。"),
    h("div", { class: "row-2" }, field("小模型", smallSel), field("看图大模型", vlmSel)),
    field("YOLO 权重路径", path, "手动指定 .pt 文件。也可把权重放到 data/models/，会自动出现在小模型列表。"),
    field("让大模型协助配置", ask),
    note,
    h(
      "div",
      { class: "form-actions" },
      h(
        "button",
        {
          class: "btn",
          type: "button",
          onclick: async () => {
            const text = ask.value.trim();
            if (!text) {
              toast("先写一下检测需求", "error");
              return;
            }
            try {
              const sug = await api("/api/vision/model-settings/assist", { method: "POST", body: { instruction: text } });
              small = sug.small_model || small;
              vlm = sug.vlm_model || "";
              if (sug.yolo_path) path.value = sug.yolo_path;
              smallSel.value = small;
              vlmSel.value = vlm;
              fill(note, sug.note || (sug.model_used ? "已根据大模型建议填好，确认后保存。" : "已按关键词填好，确认后保存。"));
              toast("已填入建议，请确认后保存", "ok");
            } catch (err) {
              toast(err.message, "error");
            }
          },
        },
        "大模型协助",
      ),
      h(
        "button",
        {
          class: "btn btn-primary",
          type: "button",
          onclick: async () => {
            try {
              const saved = await api("/api/vision/model-settings", {
                method: "PUT",
                body: { small_model: smallSel.value || "demo", yolo_path: path.value.trim(), vlm_model: vlmSel.value || "" },
              });
              toast("视觉模型已保存", "ok");
              paintVisionModelCard(root, saved);
            } catch (err) {
              toast(err.message, "error");
            }
          },
        },
        "保存视觉模型",
      ),
    ),
  );
}

async function testProvider(id, slot) {
  fill(slot, spinner(12), " 测试中…");
  try {
    const r = await api(`/api/providers/${id}/test`, { method: "POST", body: {} });
    fill(slot, 
      r.ok
        ? h("span", { class: "ok-text" }, icon("check", 13), ` ${r.model} 可用 · ${r.latency_ms}ms${r.dims ? ` · ${r.dims} 维` : ""}`)
        : h("span", { class: "text-danger", title: r.error }, icon("x", 13), ` ${r.error.slice(0, 80)}`),
    );
    return r;
  } catch (err) {
    fill(slot, h("span", { class: "text-danger" }, err.message));
    return { ok: false };
  }
}

function providerModal(preset, existing, reload) {
  const isNew = !existing;
  const models = structuredClone(existing?.models || preset.models || []);
  const name = h("input", { class: "input", value: existing?.name || preset.name });
  const key = h("input", {
    class: "input",
    type: "password",
    autocomplete: "off",
    placeholder: existing?.has_key ? `已保存 ${existing.api_key_masked}，留空表示不修改` : preset.needs_key === false ? "本地或自建服务一般不需要" : "粘贴 API Key",
  });
  const base = h("input", { class: "input mono", value: existing?.base_url || preset.base_url, placeholder: "https://…/v1" });
  const headers = h("textarea", { class: "textarea mono", rows: 3, placeholder: '{"HTTP-Referer": "https://example.com"}' });
  headers.value = existing && Object.keys(existing.extra_headers || {}).length ? JSON.stringify(existing.extra_headers, null, 2) : "";
  const rows = h("div", { class: "model-rows" });
  const result = h("div", { class: "test-result" });

  const drawRows = () => {
    fill(rows, 
      ...models.map((m, i) => {
        const type = h("select", { class: "select select-sm", onchange: (e) => ((m.type = e.target.value), drawRows()) }, ...Object.entries(TYPE_LABEL).map(([k, v]) => h("option", { value: k, selected: m.type === k }, v)));
        return h(
          "div",
          { class: "model-row" },
          h("input", { class: "input input-sm mono", value: m.id, placeholder: "模型名称", oninput: (e) => (m.id = e.target.value) }),
          type,
          m.type === "chat" ? h("label", { class: "mini-check", title: "模型支持 Function Calling 时可用于工具调用模式" }, h("input", { type: "checkbox", checked: m.tools !== false, onchange: (e) => (m.tools = e.target.checked) }), "工具调用") : h("span", { class: "mini-check muted" }, m.type === "rerank" ? m.format || "cohere" : ""),
          h("button", { class: "icon-btn", type: "button", title: "移除", onclick: () => (models.splice(i, 1), drawRows()) }, icon("x", 14)),
        );
      }),
      h("button", { class: "btn btn-sm btn-ghost", type: "button", onclick: () => (models.push({ id: "", type: "chat", tools: true }), drawRows()) }, icon("plus", 14), "添加模型"),
    );
  };
  drawRows();

  const fetchBtn = h("button", { class: "btn btn-sm", type: "button", disabled: isNew || undefined, title: isNew ? "保存后可以从服务商接口拉取" : "从服务商接口拉取可用模型" }, icon("refresh", 14), "从接口获取");
  fetchBtn.addEventListener("click", async () => {
    fetchBtn.disabled = true;
    try {
      const res = await api(`/api/providers/${existing.id}/models/fetch`, { method: "POST" });
      pickModels(res.models, models, drawRows);
    } catch (err) {
      toast(err.message, "error", 4200);
    } finally {
      fetchBtn.disabled = false;
    }
  });

  const payload = () => {
    let extra = {};
    if (headers.value.trim()) {
      try {
        extra = JSON.parse(headers.value);
      } catch {
        throw new Error("自定义请求头需要是 JSON 对象");
      }
    }
    const body = { name: name.value.trim(), base_url: base.value.trim(), models: models.filter((m) => m.id.trim()).map((m) => ({ ...m, id: m.id.trim() })), extra_headers: extra };
    if (key.value.trim()) body.api_key = key.value.trim();
    return body;
  };

  const actions = [];
  if (!isNew) {
    actions.push({
      label: "测试连接",
      onClick: async () => {
        await api(`/api/providers/${existing.id}`, { method: "PATCH", body: payload() });
        await testProvider(existing.id, result);
        return false;
      },
    });
  }
  actions.push({ label: "取消" });
  actions.push({
    label: isNew ? "保存并测试" : "保存",
    kind: "primary",
    onClick: async () => {
      const body = payload();
      const saved = isNew ? await api("/api/providers", { method: "POST", body: { preset: preset.key, ...body } }) : await api(`/api/providers/${existing.id}`, { method: "PATCH", body });
      await loadModels();
      reload();
      if (isNew) {
        const slot = h("span");
        const r = await testProvider(saved.id, slot);
        toast(r.ok ? `${saved.name} 接入成功` : `已保存，但连接测试失败：${r.error || ""}`, r.ok ? "ok" : "error", 5000);
      } else toast("已保存", "ok");
    },
  });

  modal({
    title: isNew ? `接入 ${preset.name}` : `编辑 ${existing.name}`,
    width: 660,
    body: h(
      "div",
      { class: "form" },
      preset.note ? h("div", { class: "notice" }, icon("info", 16), preset.note) : null,
      h("div", { class: "row-2" }, field("显示名称", name), field("协议", h("div", { class: "input static" }, { openai: "OpenAI 兼容", anthropic: "Anthropic Messages", gemini: "Google Gemini" }[preset.kind]))),
      field("API Key", key, preset.key_url ? h("a", { class: "link", href: preset.key_url, target: "_blank", rel: "noopener" }, "去控制台获取 API Key ", icon("external", 12)) : null),
      field("接口地址（Base URL）", base, preset.kind === "openai" ? "会请求 {Base URL}/chat/completions 和 /embeddings" : null),
      h("div", { class: "field" }, h("div", { class: "field-head" }, h("label", { class: "field-label" }, "模型"), fetchBtn), rows, h("div", { class: "hint" }, "对话模型用于回答；向量模型用于知识库检索；重排模型用于检索结果精排。厂商上新模型后，点「从接口获取」即可。")),
      h("details", { class: "advanced" }, h("summary", null, "高级：自定义请求头"), headers),
      result,
    ),
    actions,
  });
}

function pickModels(found, models, redraw) {
  const existing = new Set(models.map((m) => `${m.type}:${m.id}`));
  const picked = new Set();
  const filter = h("input", { class: "input", placeholder: `共 ${found.length} 个模型，输入关键字筛选` });
  const list = h("div", { class: "pick-list" });
  const draw = () => {
    const q = filter.value.trim().toLowerCase();
    fill(list, 
      ...found
        .filter((m) => !q || m.id.toLowerCase().includes(q))
        .slice(0, 300)
        .map((m) => {
          const already = existing.has(`${m.type}:${m.id}`);
          return h(
            "label",
            { class: `check-item${already ? " disabled" : ""}` },
            h("input", { type: "checkbox", checked: already || picked.has(m.id), disabled: already || undefined, onchange: (e) => (e.target.checked ? picked.add(m.id) : picked.delete(m.id)) }),
            h("span", { class: "check-main" }, h("b", { class: "mono" }, m.id), h("span", { class: "muted small" }, TYPE_LABEL[m.type])),
          );
        }),
    );
  };
  filter.addEventListener("input", draw);
  draw();
  modal({
    title: "选择要添加的模型",
    width: 560,
    body: h("div", null, filter, list),
    actions: [
      { label: "取消" },
      {
        label: "添加选中",
        kind: "primary",
        onClick: () => {
          for (const m of found) if (picked.has(m.id)) models.push({ id: m.id, type: m.type, tools: true });
          redraw();
        },
      },
    ],
  });
}

async function membersTab(root) {
  fill(root, h("div", { class: "loading-row" }, spinner(16), "加载成员…"));
  let users;
  try {
    ({ users } = await api("/api/users"));
  } catch (err) {
    fill(root, emptyState("alert", "加载失败", err.message));
    return;
  }
  const reload = () => membersTab(root);
  const add = () => {
    const username = h("input", { class: "input", placeholder: "登录用的账号" });
    const display = h("input", { class: "input", placeholder: "显示名称" });
    const password = h("input", { class: "input", type: "text", value: Math.random().toString(36).slice(2, 10), placeholder: "初始密码" });
    let role = "member";
    modal({
      title: "添加成员",
      body: h("div", { class: "form" }, h("div", { class: "row-2" }, field("账号", username), field("显示名称", display)), field("初始密码", password, "请把账号和初始密码发给同事，登录后可在个人设置里修改。"), field("角色", segmented([["member", "成员"], ["admin", "管理员"]], role, (v) => (role = v)))),
      actions: [
        { label: "取消" },
        {
          label: "添加",
          kind: "primary",
          onClick: async () => {
            await api("/api/users", { method: "POST", body: { username: username.value.trim(), display_name: display.value.trim(), password: password.value, role } });
            toast("已添加", "ok");
            reload();
          },
        },
      ],
    });
  };
  const rows = users.map((u) => {
    const me = u.id === store.user.id;
    const roleSel = h(
      "select",
      {
        class: "select select-sm",
        disabled: me || undefined,
        onchange: async (e) => {
          try {
            await api(`/api/users/${u.id}`, { method: "PATCH", body: { role: e.target.value } });
            toast("角色已更新", "ok");
          } catch (err) {
            toast(err.message, "error");
            reload();
          }
        },
      },
      h("option", { value: "member", selected: u.role === "member" }, "成员"),
      h("option", { value: "admin", selected: u.role === "admin" }, "管理员"),
    );
    return h(
      "tr",
      null,
      h("td", null, h("div", { class: "user-cell" }, h("span", { class: "user-avatar sm" }, (u.display_name || u.username).slice(0, 1).toUpperCase()), h("div", null, h("b", null, u.display_name), me ? badge("我", "accent") : null, h("div", { class: "muted small" }, u.username)))),
      h("td", null, roleSel),
      h("td", null, u.disabled ? badge("已停用", "danger") : badge("正常", "ok")),
      h("td", { class: "muted" }, u.last_login_at ? timeAgo(u.last_login_at) : "从未登录"),
      h("td", { class: "muted" }, fmtDate(u.created_at)),
      h(
        "td",
        { class: "row-actions" },
        me
          ? null
          : h(
              "button",
              {
                class: "btn btn-sm btn-ghost",
                type: "button",
                onclick: async () => {
                  const pwd = Math.random().toString(36).slice(2, 10);
                  const ok = await confirmDialog({ title: "重置密码", message: `把 ${u.display_name} 的密码重置为 ${pwd}？对方当前的登录会失效。`, confirmText: "重置" });
                  if (!ok) return;
                  await api(`/api/users/${u.id}`, { method: "PATCH", body: { password: pwd } });
                  copyText(pwd, `新密码 ${pwd} 已复制`);
                },
              },
              "重置密码",
            ),
        me
          ? null
          : h(
              "button",
              {
                class: `btn btn-sm ${u.disabled ? "" : "btn-danger"}`,
                type: "button",
                onclick: async () => {
                  try {
                    await api(`/api/users/${u.id}`, { method: "PATCH", body: { disabled: !u.disabled } });
                    reload();
                  } catch (err) {
                    toast(err.message, "error");
                  }
                },
              },
              u.disabled ? "启用" : "停用",
            ),
      ),
    );
  });
  fill(root, 
    h("div", { class: "toolbar" }, h("div", null, h("b", null, `${users.length} 位成员`), h("div", { class: "muted small" }, "成员可以使用团队可见的智能体和知识库，也能创建自己的；管理员还能配置模型和查看运营看板。")), h("div", { class: "grow" }), h("button", { class: "btn btn-primary", type: "button", onclick: add }, icon("plus", 15), "添加成员")),
    h("div", { class: "table-wrap" }, h("table", { class: "table" }, h("thead", null, h("tr", null, h("th", null, "成员"), h("th", null, "角色"), h("th", null, "状态"), h("th", null, "最近登录"), h("th", null, "加入时间"), h("th", null, ""))), h("tbody", null, ...rows))),
  );
}

async function integrationsTab(root) {
  fill(root, h("div", { class: "loading-row" }, spinner(16), "加载中…"));
  let keys;
  try {
    ({ keys } = await api("/api/apikeys"));
  } catch (err) {
    fill(root, emptyState("alert", "加载失败", err.message));
    return;
  }
  const reload = () => integrationsTab(root);
  const origin = location.origin;
  const agent = store.agents[0];
  const eventAgent = store.agents.find((a) => a.allow_events) || agent;
  const create = () => {
    const name = h("input", { class: "input", placeholder: "例如：监控系统、企业微信机器人、Cursor" });
    modal({
      title: "创建 API Key",
      body: field("用途", name, "Key 只显示一次，请妥善保存。它拥有和你账号相同的访问权限。"),
      actions: [
        { label: "取消" },
        {
          label: "创建",
          kind: "primary",
          onClick: async () => {
            const res = await api("/api/apikeys", { method: "POST", body: { name: name.value.trim() || "未命名" } });
            modal({
              title: "请保存这个 Key",
              body: h("div", null, h("div", { class: "code-box key-box" }, res.key, h("button", { class: "icon-btn copy", type: "button", onclick: () => copyText(res.key) }, icon("copy", 15))), h("p", { class: "hint" }, "关闭后无法再次查看完整 Key。")),
              actions: [{ label: "已保存", kind: "primary" }],
            });
            reload();
          },
        },
      ],
    });
  };
  const code = (text) => h("div", { class: "code-box" }, text, h("button", { class: "icon-btn copy", type: "button", title: "复制", onclick: () => copyText(text) }, icon("copy", 15)));
  const openaiCurl = `curl ${origin}/v1/chat/completions \\
  -H "Authorization: Bearer <API Key>" \\
  -H "Content-Type: application/json" \\
  -d '{"model": "agent-${agent ? agent.id : 1}", "messages": [{"role": "user", "content": "${agent?.suggestions?.[0] || "你好"}"}]}'`;
  const eventCurl = `curl -X POST ${origin}/api/v1/agents/${eventAgent ? eventAgent.id : 1}/events \\
  -H "Authorization: Bearer <API Key>" \\
  -H "Content-Type: application/json" \\
  -d '{"title": "web-01 CPU 使用率 96%", "level": "P2", "source": "Prometheus", "data": {"host": "web-01"}}'`;
  const mcpJson = JSON.stringify({ mcpServers: { youju: { url: `${origin}/mcp`, headers: { Authorization: "Bearer <API Key>" } } } }, null, 2);
  fill(root, 
    h(
      "section",
      { class: "card" },
      h("div", { class: "section-title" }, icon("key", 16), "API Key", h("span", { class: "grow" }), h("button", { class: "btn btn-primary btn-sm", type: "button", onclick: create }, icon("plus", 14), "创建")),
      keys.length
        ? h(
            "div",
            { class: "list" },
            ...keys.map((k) =>
              h(
                "div",
                { class: "list-row" },
                icon("key", 16, "muted"),
                h("div", { class: "grow" }, h("b", null, k.name), h("div", { class: "muted small mono" }, `${k.prefix}…`)),
                h("span", { class: "muted small" }, k.last_used_at ? `最近使用 ${timeAgo(k.last_used_at)}` : "尚未使用"),
                h(
                  "button",
                  {
                    class: "btn btn-sm btn-danger",
                    type: "button",
                    onclick: async () => {
                      const ok = await confirmDialog({ title: "吊销 Key", message: `吊销「${k.name}」后，使用它的系统会立即无法访问。`, confirmText: "吊销", danger: true });
                      if (!ok) return;
                      await api(`/api/apikeys/${k.id}`, { method: "DELETE" });
                      reload();
                    },
                  },
                  "吊销",
                ),
              ),
            ),
          )
        : h("p", { class: "muted", style: { margin: 0 } }, "还没有 API Key。下面三种接入方式都需要一个 Key。"),
    ),
    h(
      "section",
      { class: "card" },
      h("div", { class: "section-title" }, icon("link", 16), "OpenAI 兼容接口", badge("任何 OpenAI SDK / 客户端", "plain")),
      h("p", { class: "muted", style: { marginTop: 0 } }, "把智能体当作模型调用。Base URL 填下面的地址，model 填 agent-<ID>，回答末尾会附上参考资料，响应里的 youju 字段带引用和核验结果。可以接 LobeChat、Cherry Studio、企业微信机器人或自己的业务系统。"),
      h("div", { class: "kv" }, h("div", { class: "kv-k" }, "Base URL"), h("div", { class: "kv-v mono" }, `${origin}/v1`), h("div", { class: "kv-k" }, "可用模型"), h("div", { class: "kv-v" }, store.agents.map((a) => `agent-${a.id}（${a.name}）`).join("、"))),
      code(openaiCurl),
    ),
    h("section", { class: "card" }, h("div", { class: "section-title" }, icon("webhook", 16), "事件接入（Webhook）"), h("p", { class: "muted", style: { marginTop: 0 } }, "监控、检测、工单系统把事件推送过来，智能体会生成带引用的处置建议，在「事件中心」等待人工确认。智能体需要先开启「允许外部系统推送事件」。"), code(eventCurl)),
    h(
      "section",
      { class: "card" },
      h("div", { class: "section-title" }, icon("plug", 16), "MCP 服务", badge("Streamable HTTP", "plain")),
      h("p", { class: "muted", style: { marginTop: 0 } }, "在 Cursor、Claude Desktop 等 MCP 客户端里添加下面的配置，就能直接调用 list_knowledge_bases、search_knowledge 和 ask_agent 三个工具。"),
      code(mcpJson),
    ),
  );
}

function profileTab(root) {
  const user = store.user;
  const display = h("input", { class: "input", value: user.display_name, maxlength: 32 });
  const oldPwd = h("input", { class: "input", type: "password", autocomplete: "current-password" });
  const newPwd = h("input", { class: "input", type: "password", autocomplete: "new-password", placeholder: "至少 6 位" });
  fill(root, 
    h(
      "section",
      { class: "card form" },
      h("div", { class: "section-title" }, icon("user", 16), "个人资料"),
      h("div", { class: "row-2" }, field("账号", h("div", { class: "input static" }, user.username)), field("显示名称", display)),
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
                store.user = await api("/api/me", { method: "PATCH", body: { display_name: display.value.trim() } });
                toast("已保存，刷新后侧边栏会更新", "ok");
              } catch (err) {
                toast(err.message, "error");
              }
            },
          },
          "保存",
        ),
      ),
    ),
    h(
      "section",
      { class: "card form" },
      h("div", { class: "section-title" }, icon("lock", 16), "修改密码"),
      h("div", { class: "row-2" }, field("当前密码", oldPwd), field("新密码", newPwd)),
      h(
        "div",
        { class: "form-actions" },
        h(
          "button",
          {
            class: "btn",
            type: "button",
            onclick: async () => {
              try {
                await api("/api/me", { method: "PATCH", body: { old_password: oldPwd.value, new_password: newPwd.value } });
                oldPwd.value = "";
                newPwd.value = "";
                toast("密码已修改", "ok");
              } catch (err) {
                toast(err.message, "error");
              }
            },
          },
          "修改密码",
        ),
      ),
    ),
    h("section", { class: "card form" }, h("div", { class: "section-title" }, icon("sun", 16), "外观"), segmented([["light", "浅色"], ["dark", "深色"], ["system", "跟随系统"]], themePref(), (v) => applyTheme(v))),
    h("p", { class: "muted small" }, `有据 YouJu v${store.version} · 接口文档 `, h("a", { class: "link", href: "/docs", target: "_blank" }, "/docs")),
  );
}
