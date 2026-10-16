import { api, streamSSE } from "../api.js";
import { fill, confirmDialog, copyText, emptyState, h, icon, menu, modal, openSidebar, promptDialog, toast } from "../ui.js?v=20261016i";
import { isAdmin, loadAgents, loadConversations, loadModels, store } from "../state.js";
import { navigate, replacePath } from "../router.js";
import { stripCitations, usedNumbers } from "../markdown.js";
import { avatar, checkBadge, citedSources, fillMarkdown, markdownEl, modeBadges, processBlock, reasoningBlock, sourceCard, sourcesRow } from "../components.js";

const LOCAL = "local:extractive";
const OFFLINE_INFO = { ref: LOCAL, model: "离线摘录", provider: "内置", color: "#64748b", local: true, label: "离线摘录" };

export function chatView({ params, query }) {
  const st = { convId: params.id ? Number(params.id) : null, agent: null, messages: [], streaming: false, controller: null, model: LOCAL };
  let paintFrame = 0;
  const root = h("div", { class: "chat" });
  const header = h("header", { class: "topbar" });
  const thread = h("div", { class: "thread" });
  const scroller = h("div", { class: "chat-scroll" }, thread);
  const jump = h("button", { class: "jump-bottom", type: "button", hidden: true, title: "回到底部", onclick: () => scrollBottom() }, icon("arrowDown", 18));
  const composer = createComposer();
  const drawer = h("aside", { class: "drawer", hidden: true });
  root.append(h("section", { class: "chat-main" }, header, scroller, jump, composer.el), drawer);
  scroller.addEventListener("scroll", () => {
    jump.hidden = nearBottom();
  });
  init();
  return { el: root, destroy: () => st.controller?.abort() };

  async function init() {
    try {
      await Promise.all([loadAgents(), loadModels()]);
    } catch {
      /* 列表刷新失败时沿用已有缓存 */
    }
    if (!store.agents.length) {
      thread.classList.add("is-empty");
      fill(thread, emptyState("bot", "还没有可用的智能体", "先创建一个智能体，绑定知识库后就能开始提问。", h("button", { class: "btn btn-primary", type: "button", onclick: () => navigate("/agents/new") }, icon("plus", 16), "创建智能体")));
      composer.el.hidden = true;
      renderHeader();
      return;
    }
    if (st.convId) {
      try {
        const data = await api(`/api/conversations/${st.convId}`);
        st.messages = data.messages;
        st.agent = store.agents.find((a) => a.id === data.conversation.agent_id) || null;
      } catch (err) {
        thread.classList.add("is-empty");
        fill(thread, emptyState("alert", "打不开这个对话", err.message, h("button", { class: "btn btn-primary", type: "button", onclick: () => navigate("/chat") }, "新建对话")));
        composer.el.hidden = true;
        renderHeader();
        return;
      }
    } else {
      const wanted = Number(query.agent) || Number(localStorage.getItem("yj-last-agent"));
      st.agent = store.agents.find((a) => a.id === wanted) || store.agents[0];
    }
    if (st.agent) {
      localStorage.setItem("yj-last-agent", String(st.agent.id));
      st.model = pickModel(st.agent);
    }
    renderHeader();
    renderThread();
    composer.update();
    scrollBottom();
    if (!st.convId) composer.focus();
  }

  function pickModel(agent) {
    const refs = new Set(store.models.chat.map((m) => m.ref));
    for (const ref of [localStorage.getItem(`yj-model-${agent.id}`), agent.model_ref, store.models.defaults?.default_chat_model]) {
      if (ref && refs.has(ref)) return ref;
    }
    return store.models.chat.find((m) => !m.local)?.ref || LOCAL;
  }

  function modelInfo() {
    return store.models.chat.find((m) => m.ref === st.model) || OFFLINE_INFO;
  }

  function renderHeader() {
    const info = modelInfo();
    fill(header, 
      h("button", { class: "icon-btn only-mobile", type: "button", title: "菜单", onclick: openSidebar }, icon("menu")),
      st.agent
        ? h("button", { class: "agent-switch", type: "button", title: "切换智能体", onclick: (e) => agentMenu(e.currentTarget) }, avatar(st.agent, "sm"), h("span", { class: "agent-switch-name" }, st.agent.name), icon("chevronDown", 16, "muted"))
        : h("div", { class: "agent-switch is-static" }, st.convId ? "智能体已删除" : ""),
      h("div", { class: "spacer" }),
      st.agent
        ? h("button", { class: "model-pick", type: "button", title: "切换模型", onclick: (e) => modelMenu(e.currentTarget) }, h("span", { class: "model-dot", style: { background: info.color } }), h("span", { class: "model-name" }, info.local ? "离线摘录" : info.model), icon("chevronDown", 14, "muted"))
        : null,
      st.convId ? h("button", { class: "icon-btn", type: "button", title: "更多", onclick: (e) => moreMenu(e.currentTarget) }, icon("more")) : null,
    );
  }

  function agentMenu(anchor) {
    menu(
      anchor,
      [
        { header: true, label: "切换智能体会开始新对话" },
        ...store.agents.map((a) => ({
          lead: avatar(a, "sm"),
          label: a.name,
          sub: a.category,
          active: a.id === st.agent?.id,
          onClick: () => navigate(`/chat?agent=${a.id}`),
        })),
        { sep: true },
        { icon: "bot", label: "管理智能体", onClick: () => navigate("/agents") },
      ],
      { width: 280 },
    );
  }

  function modelMenu(anchor) {
    const groups = new Map();
    for (const m of store.models.chat) {
      const key = m.local ? "内置" : m.provider;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(m);
    }
    const items = [];
    for (const [name, models] of groups) {
      items.push({ header: true, label: name });
      for (const m of models) {
        items.push({
          lead: h("span", { class: "model-dot", style: { background: m.color } }),
          label: m.local ? "离线摘录（不调用模型）" : m.model,
          sub: m.local ? "直接摘录资料原文，没有模型时也能用" : m.tools ? "支持工具调用" : "检索增强",
          active: m.ref === st.model,
          onClick: async () => {
            st.model = m.ref;
            localStorage.setItem(`yj-model-${st.agent.id}`, m.ref);
            renderHeader();
            if (st.agent?.can_edit) {
              try {
                const updated = await api(`/api/agents/${st.agent.id}/model`, {
                  method: "PATCH",
                  body: { model_ref: m.local ? null : m.ref },
                });
                st.agent = { ...st.agent, ...updated };
                const row = store.agents.find((a) => a.id === st.agent.id);
                if (row) row.model_ref = updated.model_ref;
                toast(`已将「${st.agent.name}」的对话模型改为${m.local ? "离线摘录" : m.model}`, "ok");
              } catch (err) {
                toast(err.message || "模型已在本次对话切换，但没有写回智能体", "error");
              }
            }
            if (!st.messages.length) renderThread();
          },
        });
      }
    }
    if (store.models.chat.length === 1) items.push({ header: true, label: "还没有接入大模型" });
    if (isAdmin()) items.push({ sep: true }, { icon: "plug", label: "接入更多模型", onClick: () => navigate("/settings/models") });
    menu(anchor, items, { align: "right", width: 290 });
  }

  function moreMenu(anchor) {
    menu(
      anchor,
      [
        { icon: "edit", label: "重命名", onClick: rename },
        { icon: "download", label: "导出 Markdown", onClick: () => (location.href = `/api/conversations/${st.convId}/export`) },
        { sep: true },
        { icon: "trash", label: "删除对话", danger: true, onClick: remove },
      ],
      { align: "right" },
    );
  }

  async function rename() {
    const current = store.conversations.find((c) => c.id === st.convId);
    const title = await promptDialog({ title: "重命名对话", label: "标题", value: current?.title || "" });
    if (!title) return;
    await api(`/api/conversations/${st.convId}`, { method: "PATCH", body: { title } });
    loadConversations();
  }

  async function remove() {
    const ok = await confirmDialog({ title: "删除对话", message: "删除后无法恢复。", confirmText: "删除", danger: true });
    if (!ok) return;
    await api(`/api/conversations/${st.convId}`, { method: "DELETE" });
    await loadConversations();
    navigate("/chat");
  }

  function welcome() {
    const a = st.agent;
    const offline = modelInfo().local;
    return h(
      "div",
      { class: "welcome" },
      h("div", { class: "avatar-xl", style: { background: `color-mix(in srgb, ${a.color} 16%, transparent)` } }, a.emoji),
      h("h1", null, a.name),
      h("p", { class: "welcome-desc" }, a.opening || a.description),
      h(
        "div",
        { class: "welcome-meta" },
        a.kbs.length ? h("span", { class: "chip" }, icon("book", 14), a.kbs.map((k) => k.name).join("、")) : h("span", { class: "chip" }, icon("sparkles", 14), "通用对话"),
        a.kbs.length ? h("span", { class: `chip${a.strict ? " chip-accent" : ""}` }, icon("shield", 14), a.strict ? "严格依据：没有依据不作答" : "无依据时会先说明") : null,
        offline ? h("span", { class: "chip chip-warn", title: "管理员接入模型后会自动切换" }, icon("info", 14), "未接入大模型，当前为离线摘录") : null,
      ),
      a.suggestions?.length
        ? h("div", { class: "suggest-grid" }, ...a.suggestions.slice(0, 4).map((text) => h("button", { class: "suggest", type: "button", onclick: () => send(text) }, h("span", null, text), icon("arrowUpRight", 16))))
        : null,
    );
  }

  function renderThread() {
    if (!st.messages.length && st.agent) {
      thread.classList.add("is-empty");
      fill(thread, welcome());
      return;
    }
    thread.classList.remove("is-empty");
    fill(thread, ...st.messages.map((m) => (m.role === "user" ? userMessage(m) : aiMessage(m))));
  }

  function userMessage(m) {
    const files = m.attachments || [];
    const el = h(
      "div",
      { class: "msg msg-user" },
      files.length
        ? h(
            "div",
            { class: "msg-files" },
            ...files.map((file) =>
              file.kind === "images"
                ? h("img", { class: "msg-file-img", src: file.url, alt: file.name, title: file.name })
                : h("a", { class: "msg-file", href: file.url, target: "_blank", rel: "noreferrer" }, icon("file", 14), file.name),
            ),
          )
        : null,
      h("div", { class: "bubble" }, m.content),
    );
    m._el = el;
    return el;
  }

  function isLastAnswer(m) {
    return st.messages[st.messages.length - 1] === m;
  }

  function aiMessage(m) {
    const agent = st.agent;
    const cited = citedSources(m.content, m.sources);
    const label = m.mode === "offline" ? "" : m.model?.label || m.model_label || "";
    const body = h(
      "div",
      { class: "msg-body" },
      h("div", { class: "msg-meta" }, h("span", { class: "msg-name" }, agent?.name || "助手"), label ? h("span", { class: "msg-model" }, label) : null, ...modeBadges(m)),
    );
    if (m.reasoning) body.append(reasoningBlock(m.reasoning, { streaming: m.streaming && !m.content, open: m._openReasoning, onToggle: (v) => (m._openReasoning = v) }));
    if (m.trace?.length) body.append(processBlock(m.trace, { streaming: m.streaming, open: m._openProcess, onToggle: (v) => (m._openProcess = v) }));
    if (m.content || !m.streaming) {
      const content = markdownEl(m.content, m.sources, m.streaming);
      content.addEventListener("click", (e) => {
        const cite = e.target.closest(".cite");
        if (cite) openDrawer(m, Number(cite.dataset.n));
        const copy = e.target.closest(".code-copy");
        if (copy) copyText(copy.closest(".code-block").querySelector("code").textContent);
      });
      body.append(content);
    } else if (!m.trace?.length) {
      body.append(h("div", { class: "typing" }, h("i"), h("i"), h("i")));
    }
    if (m.error) body.append(h("div", { class: "msg-error" }, icon("alert", 16), h("span", null, m.error)));
    if (cited.length) body.append(sourcesRow(cited, (n) => openDrawer(m, n)));
    if (!m.streaming) {
      body.append(
        h(
          "div",
          { class: "msg-actions" },
          checkBadge(m.check),
          actionButton("copy", "复制", () => copyText(stripCitations(m.content))),
          isLastAnswer(m) && st.agent ? actionButton("refresh", "重新生成", () => send("", true)) : null,
          m.id ? actionButton("thumbUp", "有帮助", () => rate(m, m.feedback === 1 ? 0 : 1), m.feedback === 1) : null,
          m.id ? actionButton("thumbDown", "没帮助", () => rateDown(m), m.feedback === -1) : null,
          m.sources?.length && !cited.length ? actionButton("book", "查看检索结果", () => openDrawer(m, m.sources[0].n)) : null,
          m.latency_ms ? h("span", { class: "msg-stat", title: "总耗时" }, `${(m.latency_ms / 1000).toFixed(1)}s`) : null,
        ),
      );
    }
    const el = h("div", { class: "msg msg-ai" }, h("div", { class: "msg-avatar", style: { background: `color-mix(in srgb, ${agent?.color || "#64748b"} 16%, transparent)` } }, agent?.emoji || "✨"), body);
    m._el = el;
    return el;
  }

  function actionButton(name, title, onclick, on = false) {
    return h("button", { class: `icon-btn${on ? " on" : ""}`, type: "button", title, onclick }, icon(name, 16));
  }

  function schedulePaint(m) {
    if (paintFrame) return;
    paintFrame = requestAnimationFrame(() => {
      paintFrame = 0;
      paint(m);
    });
  }

  function paint(m) {
    const stick = nearBottom();
    if (m.role === "assistant" && m._el?.isConnected) patchAssistant(m);
    else mountMessage(m);
    if (stick && scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight > 2) scrollBottom();
  }

  function mountMessage(m) {
    const fresh = m.role === "user" ? userMessage(m) : aiMessage(m);
    if (m._old && m._old.isConnected) {
      fresh.classList.add("is-settled");
      m._old.replaceWith(fresh);
    } else if (!fresh.isConnected) thread.append(fresh);
    m._old = fresh;
    m._el = fresh;
  }

  function placeBefore(body, el, selector) {
    const before = body.querySelector(selector);
    if (before) body.insertBefore(el, before);
    else body.append(el);
  }

  function bindMarkdown(md, m) {
    md.addEventListener("click", (e) => {
      const cite = e.target.closest(".cite");
      if (cite) openDrawer(m, Number(cite.dataset.n));
      const copy = e.target.closest(".code-copy");
      if (copy) copyText(copy.closest(".code-block").querySelector("code").textContent);
    });
  }

  function patchAssistant(m) {
    const body = m._el.querySelector(".msg-body");
    if (!body) return mountMessage(m);
    m._el.classList.add("is-settled");
    syncReasoning(body, m);
    syncProcess(body, m);
    syncContent(body, m);
    syncError(body, m);
    syncSources(body, m);
    syncActions(body, m);
  }

  function syncReasoning(body, m) {
    let box = body.querySelector(".reasoning");
    if (!m.reasoning) {
      box?.remove();
      return;
    }
    const thinking = !!(m.streaming && !m.content);
    if (!box) {
      box = reasoningBlock(m.reasoning, { streaming: thinking, open: m._openReasoning, onToggle: (v) => (m._openReasoning = v) });
      placeBefore(body, box, ".process, .md, .typing, .msg-error, .sources, .msg-actions");
      return;
    }
    const text = box.querySelector(".reasoning-body");
    if (text && text.textContent !== m.reasoning) text.textContent = m.reasoning;
    const label = box.querySelector(".reasoning-label");
    const next = thinking ? "正在思考…" : "已深度思考";
    if (label && label.textContent !== next) label.textContent = next;
  }

  function syncProcess(body, m) {
    let box = body.querySelector(".process");
    if (!m.trace?.length) {
      box?.remove();
      return;
    }
    const sig = JSON.stringify(m.trace);
    if (!box) {
      box = processBlock(m.trace, { streaming: m.streaming, open: m._openProcess, onToggle: (v) => (m._openProcess = v) });
      box.dataset.sig = sig;
      placeBefore(body, box, ".md, .typing, .msg-error, .sources, .msg-actions");
      return;
    }
    if (box.dataset.sig === sig) return;
    const fresh = processBlock(m.trace, { streaming: m.streaming, open: box.open, onToggle: (v) => (m._openProcess = v) });
    box.querySelector("summary")?.replaceWith(fresh.querySelector("summary"));
    box.querySelector(".process-body")?.replaceWith(fresh.querySelector(".process-body"));
    box.dataset.sig = sig;
  }

  function syncContent(body, m) {
    let md = body.querySelector(".md");
    const typing = body.querySelector(".typing");
    if (m.content || !m.streaming) {
      if (!md) {
        md = markdownEl(m.content, m.sources, m.streaming);
        bindMarkdown(md, m);
        if (typing) typing.replaceWith(md);
        else placeBefore(body, md, ".msg-error, .sources, .msg-actions");
      } else fillMarkdown(md, m.content, m.sources, m.streaming);
      return;
    }
    md?.remove();
    if (!m.trace?.length && !typing) placeBefore(body, h("div", { class: "typing" }, h("i"), h("i"), h("i")), ".msg-error, .sources, .msg-actions");
  }

  function syncError(body, m) {
    let box = body.querySelector(".msg-error");
    if (!m.error) {
      box?.remove();
      return;
    }
    if (!box) {
      box = h("div", { class: "msg-error" }, icon("alert", 16), h("span", null, m.error));
      placeBefore(body, box, ".sources, .msg-actions");
      return;
    }
    const span = box.querySelector("span");
    if (span && span.textContent !== m.error) span.textContent = m.error;
  }

  function syncSources(body, m) {
    const cited = citedSources(m.content, m.sources);
    let box = body.querySelector(".sources");
    if (!cited.length) {
      box?.remove();
      return;
    }
    const sig = cited.map((s) => s.n).join(",");
    if (box?.dataset.sig === sig) return;
    const fresh = sourcesRow(cited, (n) => openDrawer(m, n));
    fresh.dataset.sig = sig;
    if (box) box.replaceWith(fresh);
    else placeBefore(body, fresh, ".msg-actions");
  }

  function syncActions(body, m) {
    body.querySelector(".msg-actions")?.remove();
    if (m.streaming) return;
    body.append(
      h(
        "div",
        { class: "msg-actions" },
        checkBadge(m.check),
        actionButton("copy", "复制", () => copyText(stripCitations(m.content))),
        isLastAnswer(m) && st.agent ? actionButton("refresh", "重新生成", () => send("", true)) : null,
        m.id ? actionButton("thumbUp", "有帮助", () => rate(m, m.feedback === 1 ? 0 : 1), m.feedback === 1) : null,
        m.id ? actionButton("thumbDown", "没帮助", () => rateDown(m), m.feedback === -1) : null,
        m.sources?.length && !citedSources(m.content, m.sources).length ? actionButton("book", "查看检索结果", () => openDrawer(m, m.sources[0].n)) : null,
        m.latency_ms ? h("span", { class: "msg-stat", title: "总耗时" }, `${(m.latency_ms / 1000).toFixed(1)}s`) : null,
      ),
    );
  }

  function nearBottom() {
    return scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 140;
  }

  function scrollBottom() {
    scroller.scrollTop = scroller.scrollHeight;
  }

  async function send(text, regenerate = false) {
    text = (text || "").trim();
    const attached = regenerate ? { ids: [], copies: [] } : composer.takeAttachments();
    if (st.streaming || !st.agent || (!text && !regenerate && !attached.ids.length)) return;
    if (!st.convId) {
      try {
        const conv = await api("/api/conversations", { method: "POST", body: { agent_id: st.agent.id } });
        st.convId = conv.id;
        replacePath(`/chat/${conv.id}`);
        renderHeader();
      } catch (err) {
        toast(err.message, "error");
        return;
      }
    }
    if (regenerate) {
      while (st.messages.length && st.messages[st.messages.length - 1].role === "assistant") st.messages.pop();
    } else {
      st.messages.push({ role: "user", content: text || "请看我上传的文件", attachments: attached.copies });
      composer.clear();
    }
    const ai = { role: "assistant", content: "", reasoning: "", sources: [], trace: [], streaming: true, model: modelInfo() };
    st.messages.push(ai);
    renderThread();
    ai._old = ai._el;
    scrollBottom();
    st.streaming = true;
    composer.update();
    const controller = new AbortController();
    st.controller = controller;
    try {
      await streamSSE(
        `/api/conversations/${st.convId}/chat`,
        { content: regenerate ? null : text, model: st.model, regenerate, attachment_ids: attached.ids },
        (ev) => onEvent(ai, ev),
        controller.signal,
      );
    } catch (err) {
      if (err.name === "AbortError") ai.stopped = true;
      else ai.error = err.message;
    } finally {
      if (!ai.content && st.convId) {
        try {
          const data = await api(`/api/conversations/${st.convId}`);
          const last = [...(data.messages || [])].reverse().find((m) => m.role === "assistant");
          if (last) Object.assign(ai, last);
        } catch {
          /* 回读失败时仍结束流式状态 */
        }
      }
      ai.streaming = false;
      st.streaming = false;
      st.controller = null;
      cancelAnimationFrame(paintFrame);
      paintFrame = 0;
      paint(ai);
      composer.update();
      loadConversations().catch(() => {});
    }
  }

  function stop() {
    st.controller?.abort();
  }

  function onEvent(ai, ev) {
    switch (ev.type) {
      case "start":
        loadConversations().catch(() => {});
        break;
      case "meta":
        ai.model = ev.model;
        ai.mode = ev.mode;
        break;
      case "step": {
        const i = ai.trace.findIndex((s) => s.id === ev.step.id);
        if (i >= 0) ai.trace[i] = ev.step;
        else ai.trace.push(ev.step);
        break;
      }
      case "reasoning":
        ai.reasoning += ev.delta;
        break;
      case "delta":
        ai.content += ev.delta;
        break;
      case "interim_reset":
        ai.content = "";
        break;
      case "replace":
        ai.content = ev.content;
        break;
      case "sources":
        ai.sources = ev.sources;
        break;
      case "error":
        ai.error = ev.message;
        break;
      case "final":
        Object.assign(ai, {
          id: ev.message_id,
          content: ev.content,
          reasoning: ev.reasoning || ai.reasoning,
          sources: ev.sources,
          check: ev.check,
          refused: ev.refused,
          mode: ev.mode,
          model: ev.model,
          usage: ev.usage,
          latency_ms: ev.latency_ms,
          trace: ev.trace,
          error: ev.error || ai.error,
        });
        break;
      default:
        return;
    }
    schedulePaint(ai);
  }

  async function rate(m, rating, note = "") {
    try {
      await api(`/api/messages/${m.id}/feedback`, { method: "POST", body: { rating, note } });
      m.feedback = rating;
      paintInPlace(m);
      if (rating === 1) toast("谢谢反馈", "ok", 1400);
      if (rating === -1) toast("已记录，管理员可以在运营看板里看到", "ok");
    } catch (err) {
      toast(err.message, "error");
    }
  }

  function paintInPlace(m) {
    m._old = m._el;
    paint(m);
  }

  function rateDown(m) {
    if (m.feedback === -1) return rate(m, 0);
    const picked = new Set();
    const reasons = ["答非所问", "引用不准确", "信息过时", "资料里没有", "表述不清楚"];
    const note = h("textarea", { class: "textarea", rows: 3, placeholder: "补充说明（可选）" });
    const chips = h(
      "div",
      { class: "chip-select" },
      ...reasons.map((reason) => {
        const chip = h("button", { class: "chip-toggle", type: "button" }, reason);
        chip.addEventListener("click", () => {
          chip.classList.toggle("on");
          if (chip.classList.contains("on")) picked.add(reason);
          else picked.delete(reason);
        });
        return chip;
      }),
    );
    modal({
      title: "这条回答哪里不对？",
      body: h("div", null, chips, note, h("p", { class: "hint" }, "差评会进入运营看板，管理员可以一键加入检索评测集。")),
      actions: [{ label: "取消" }, { label: "提交", kind: "primary", onClick: () => rate(m, -1, [...picked, note.value.trim()].filter(Boolean).join("；")) }],
    });
  }

  function openDrawer(m, focus) {
    const used = new Set(usedNumbers(m.content));
    const cited = (m.sources || []).filter((s) => used.has(s.n));
    const others = (m.sources || []).filter((s) => !used.has(s.n));
    drawer.hidden = false;
    root.classList.add("with-drawer");
    fill(drawer, 
      h(
        "div",
        { class: "drawer-head" },
        h("div", null, h("div", { class: "drawer-title" }, "引用来源"), h("div", { class: "drawer-sub" }, cited.length ? `回答引用了 ${cited.length} 段原文` : `检索到 ${others.length} 段资料，回答中没有引用`)),
        h("button", { class: "icon-btn", type: "button", title: "关闭", onclick: closeDrawer }, icon("x")),
      ),
      h(
        "div",
        { class: "drawer-body" },
        ...cited.map((s) => sourceCard(s, s.n === focus)),
        others.length
          ? h("details", { class: "others", open: !cited.length || others.some((s) => s.n === focus) || undefined }, h("summary", null, `其他检索结果 ${others.length}`), ...others.map((s) => sourceCard(s, s.n === focus)))
          : null,
      ),
    );
    requestAnimationFrame(() => drawer.querySelector(`.src-card[data-n="${focus}"]`)?.scrollIntoView({ block: "nearest", behavior: "smooth" }));
  }

  function closeDrawer() {
    drawer.hidden = true;
    root.classList.remove("with-drawer");
  }

  function createComposer() {
    const pending = [];
    let mcpClients = [];
    const input = h("textarea", { class: "composer-input", rows: 1 });
    const button = h("button", { class: "send-btn", type: "button" });
    const tools = h("div", { class: "composer-tools" });
    const files = h("div", { class: "composer-files" });
    const picker = h("input", { type: "file", multiple: true, hidden: true });
    const box = h("div", { class: "composer" }, files, input, h("div", { class: "composer-bar" }, tools, button));
    const el = h("div", { class: "composer-dock" }, box, h("div", { class: "composer-note" }, "回答由 AI 依据资料生成，关键操作请以引用原文为准"));
    const autosize = () => {
      input.style.height = "auto";
      input.style.height = `${Math.min(input.scrollHeight, 220)}px`;
      update();
    };
    input.addEventListener("input", autosize);
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
        e.preventDefault();
        send(input.value);
      }
    });
    picker.addEventListener("change", () => addFiles(picker.files));
    box.addEventListener("dragover", (e) => {
      e.preventDefault();
      box.classList.add("is-drop");
    });
    box.addEventListener("dragleave", () => box.classList.remove("is-drop"));
    box.addEventListener("drop", (e) => {
      e.preventDefault();
      box.classList.remove("is-drop");
      addFiles(e.dataTransfer?.files);
    });
    button.addEventListener("click", () => (st.streaming ? stop() : send(input.value)));
    box.addEventListener("mousedown", (e) => {
      if (e.target === box) {
        e.preventDefault();
        input.focus();
      }
    });
    function update() {
      const a = st.agent;
      input.placeholder = a ? `给「${a.name}」发消息，Enter 发送，Shift + Enter 换行` : "智能体已删除，只能查看记录";
      input.disabled = !a;
      fill(button, icon(st.streaming ? "stop" : "arrowUp", 18));
      button.title = st.streaming ? "停止生成" : "发送";
      button.disabled = !a || (!st.streaming && !input.value.trim() && !pending.length);
      button.classList.toggle("is-stop", st.streaming);
      const mcpOn = mcpClients.filter((c) => c.enabled);
      fill(tools, 
        ...(a
          ? [
              h("button", { class: "tool-chip tool-btn", type: "button", title: "上传图片或文件", onclick: () => picker.click() }, icon("paperclip", 14), "附件"),
              h("button", { class: "tool-chip tool-btn", type: "button", title: "连接外部 MCP 工具", onclick: () => openMcp() }, icon("plug", 14), mcpOn.length ? `MCP ${mcpOn.length}` : "MCP"),
              a.kbs.length ? h("span", { class: "tool-chip", title: a.kbs.map((k) => k.name).join("、") }, icon("book", 14), a.kbs.length === 1 ? a.kbs[0].name : `${a.kbs.length} 个知识库`) : h("span", { class: "tool-chip" }, icon("sparkles", 14), "通用对话"),
              a.kbs.length && a.strict ? h("span", { class: "tool-chip tool-chip-accent" }, icon("shield", 14), "严格依据") : null,
              a.strategy === "agent" ? h("span", { class: "tool-chip" }, icon("zap", 14), "工具调用") : null,
            ]
          : []),
      );
      fill(
        files,
        picker,
        ...pending.map((file, index) =>
          h(
            "span",
            { class: "composer-file" },
            file.kind === "images" ? h("img", { src: file.url, alt: "" }) : icon("file", 14),
            h("span", null, file.name),
            h(
              "button",
              {
                class: "icon-btn",
                type: "button",
                title: "移除",
                onclick: () => {
                  pending.splice(index, 1);
                  update();
                },
              },
              icon("x", 12),
            ),
          ),
        ),
      );
      files.hidden = !pending.length;
    }
    async function addFiles(list) {
      const picked = [...(list || [])];
      if (!picked.length) return;
      const form = new FormData();
      for (const file of picked) form.append("files", file);
      try {
        const res = await api("/api/chat/uploads", { method: "POST", form });
        pending.push(...(res.uploads || []));
        toast(`已上传 ${res.uploads?.length || 0} 个文件`, "ok");
        update();
      } catch (err) {
        toast(err.message, "error");
      }
      picker.value = "";
    }
    async function loadMcp() {
      try {
        mcpClients = (await api("/api/mcp/clients")).clients || [];
      } catch {
        mcpClients = [];
      }
      update();
    }
    function openMcp() {
      const name = h("input", { class: "input", placeholder: "名称，例如 文件系统", maxlength: 80 });
      const url = h("input", { class: "input mono", placeholder: "http://127.0.0.1:3000/mcp" });
      const list = h("div", { class: "mcp-list" });
      const paintList = () => {
        fill(
          list,
          mcpClients.length
            ? mcpClients.map((client) =>
                h(
                  "div",
                  { class: "mcp-row" },
                  h("div", { class: "grow" }, h("b", null, client.name), h("div", { class: "muted small" }, client.last_error || (client.tools || []).map((t) => t.name).join("、") || client.url)),
                  h(
                    "button",
                    {
                      class: `btn btn-sm${client.enabled ? " btn-primary" : ""}`,
                      type: "button",
                      onclick: async () => {
                        const updated = await api(`/api/mcp/clients/${client.id}?enabled=${client.enabled ? "false" : "true"}`, { method: "PATCH" });
                        Object.assign(client, updated);
                        paintList();
                        update();
                      },
                    },
                    client.enabled ? "已接入" : "未接入",
                  ),
                  h(
                    "button",
                    {
                      class: "icon-btn",
                      type: "button",
                      title: "删除",
                      onclick: async () => {
                        await api(`/api/mcp/clients/${client.id}`, { method: "DELETE" });
                        mcpClients = mcpClients.filter((c) => c.id !== client.id);
                        paintList();
                        update();
                      },
                    },
                    icon("x", 14),
                  ),
                ),
              )
            : h("p", { class: "muted", style: { margin: 0 } }, "还没有外部工具。填入 MCP 地址后点连接。"),
        );
      };
      paintList();
      modal({
        title: "外部 MCP 工具",
        width: 560,
        body: h(
          "div",
          { class: "form" },
          h("p", { class: "muted", style: { marginTop: 0 } }, "连接后，工具调用模式的智能体可以在对话里使用这些工具。图片和文件仍通过左侧「附件」上传。"),
          list,
          fieldRow(name, url),
        ),
        actions: [
          { label: "关闭" },
          {
            label: "连接",
            kind: "primary",
            onClick: async () => {
              if (!name.value.trim() || !url.value.trim()) {
                toast("请填写名称和地址", "error");
                return false;
              }
              const created = await api("/api/mcp/clients", { method: "POST", body: { name: name.value.trim(), url: url.value.trim() } });
              mcpClients.push(created);
              if (created.last_error) toast(created.last_error, "error");
              else toast(`已连接，${(created.tools || []).length} 个工具`, "ok");
              paintList();
              update();
              return false;
            },
          },
        ],
      });
    }
    function fieldRow(name, url) {
      return h("div", { class: "row-2" }, h("div", { class: "field" }, h("label", { class: "field-label" }, "名称"), name), h("div", { class: "field" }, h("label", { class: "field-label" }, "MCP 地址"), url));
    }
    loadMcp();
    return {
      el,
      update,
      focus: () => input.focus(),
      takeAttachments() {
        const ids = pending.map((file) => file.id);
        const copies = pending.slice();
        pending.length = 0;
        return { ids, copies };
      },
      clear: () => {
        input.value = "";
        autosize();
      },
    };
  }
}
