import { api, onUnauthorized } from "./api.js";
import { fill, applyTheme, closeSidebar, confirmDialog, h, icon, logo, menu, promptDialog, themePref, toast } from "./ui.js";
import { isAdmin, loadAll, loadConversations, on, store } from "./state.js";
import { currentPath, navigate, route, startRouter } from "./router.js";
import { renderAuth } from "./views/auth.js";
import { chatView } from "./views/chat.js";
import { agentEditView, agentsView } from "./views/agents.js";
import { kbDetailView, kbListView } from "./views/kbs.js";
import { eventsView } from "./views/events.js";
import { dashboardView } from "./views/dashboard.js";
import { settingsView } from "./views/settings.js";
import { visionView } from "./views/vision.js";

const root = document.getElementById("app");

route("/chat", chatView);
route("/chat/:id", chatView);
route("/agents", agentsView);
route("/agents/new", agentEditView);
route("/agents/:id", agentEditView);
route("/kbs", kbListView);
route("/kbs/:id", kbDetailView);
route("/vision/rules", visionView);
route("/vision", visionView);
route("/vision/:id", visionView);
route("/events", eventsView);
route("/events/:id", eventsView);
route("/dashboard", dashboardView);
route("/settings", settingsView);
route("/settings/:tab", settingsView);

const NAV = [
  { path: "/chat", label: "对话", icon: "chat" },
  { path: "/agents", label: "智能体", icon: "bot" },
  { path: "/kbs", label: "知识库", icon: "book" },
  { path: "/vision", label: "视觉检测", icon: "camera" },
  { path: "/events", label: "事件中心", icon: "inbox", badge: true },
  { path: "/dashboard", label: "运营看板", icon: "chart", admin: true },
  { path: "/settings", label: "设置", icon: "settings" },
];

async function boot() {
  applyTheme(themePref());
  let status;
  try {
    status = await api("/api/auth/status", { quiet: true });
  } catch (err) {
    fill(root, h("div", { class: "boot-error" }, icon("alert", 28), h("h2", null, "连接不上服务"), h("p", null, err.message)));
    return;
  }
  store.version = status.version;
  if (!status.user) {
    store.user = null;
    renderAuth(root, status.initialized, boot);
    return;
  }
  store.user = status.user;
  try {
    await loadAll();
  } catch (err) {
    toast(err.message, "error");
  }
  renderShell();
}

onUnauthorized(() => {
  if (store.user) {
    store.user = null;
    toast("登录已过期，请重新登录", "error");
    boot();
  }
});

function renderShell() {
  const outlet = h("main", { class: "main" });
  const shell = h("div", { class: "app" }, buildSidebar(), h("div", { class: "scrim", onclick: closeSidebar }), outlet);
  fill(root, shell);
  startRouter(outlet, (path) => {
    highlight(path);
    closeSidebar();
  });
}

function highlight(path) {
  document.querySelectorAll(".nav-item").forEach((el) => {
    const target = el.dataset.path;
    el.classList.toggle("active", target === "/chat" ? path === "/chat" : path.startsWith(target));
  });
  document.querySelectorAll(".conv-item").forEach((el) => {
    el.classList.toggle("active", path === `/chat/${el.dataset.id}`);
  });
}

function buildSidebar() {
  const list = h("div", { class: "sb-convs" });
  const nav = h(
    "nav",
    { class: "sb-nav" },
    ...NAV.filter((item) => !item.admin || isAdmin()).map((item) =>
      h(
        "a",
        { class: "nav-item", href: `#${item.path}`, dataset: { path: item.path } },
        icon(item.icon, 18),
        h("span", null, item.label),
        item.badge ? h("span", { class: "nav-badge", hidden: true }) : null,
      ),
    ),
  );
  const renderBadge = () => {
    const el = nav.querySelector(".nav-badge");
    if (!el) return;
    el.hidden = !store.pendingEvents;
    el.textContent = store.pendingEvents > 99 ? "99+" : String(store.pendingEvents);
  };
  const aside = h(
    "aside",
    { class: "sidebar" },
    h(
      "div",
      { class: "sb-top" },
      h("a", { class: "brand", href: "#/chat" }, logo(28), h("span", { class: "brand-name" }, "有据"), h("span", { class: "brand-tag" }, "YouJu")),
      h("button", { class: "icon-btn only-mobile", type: "button", title: "收起", onclick: closeSidebar }, icon("x")),
    ),
    h("button", { class: "btn new-chat", type: "button", onclick: () => navigate("/chat") }, icon("plus", 17), h("span", null, "新建对话"), h("kbd", null, "Ctrl K")),
    nav,
    h("div", { class: "sb-section" }, "最近对话"),
    list,
    userCard(),
  );
  renderConversations(list);
  on("conversations", () => renderConversations(list));
  on("events", renderBadge);
  renderBadge();
  return aside;
}

function groupOf(iso) {
  const day = new Date(iso);
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const diff = (today - new Date(day.getFullYear(), day.getMonth(), day.getDate())) / 86400000;
  if (diff <= 0) return "今天";
  if (diff <= 1) return "昨天";
  if (diff <= 7) return "近 7 天";
  return "更早";
}

function renderConversations(list) {
  if (!store.conversations.length) {
    fill(list, h("div", { class: "sb-empty" }, "还没有对话。选一个智能体开始提问吧。"));
    return;
  }
  const groups = new Map();
  for (const conv of store.conversations) {
    const key = conv.pinned ? "置顶" : groupOf(conv.updated_at);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(conv);
  }
  const path = currentPath().split("?")[0];
  fill(list, 
    ...[...groups].flatMap(([name, items]) => [
      h("div", { class: "conv-group" }, name),
      ...items.map((conv) =>
        h(
          "a",
          { class: `conv-item${path === `/chat/${conv.id}` ? " active" : ""}`, href: `#/chat/${conv.id}`, dataset: { id: String(conv.id) }, title: `${conv.agent_name} · ${conv.title}` },
          h("span", { class: "conv-emoji" }, conv.agent_emoji),
          h("span", { class: "conv-title" }, conv.title),
          h(
            "button",
            {
              class: "conv-more icon-btn",
              type: "button",
              title: "更多",
              onclick: (e) => {
                e.preventDefault();
                e.stopPropagation();
                conversationMenu(e.currentTarget, conv);
              },
            },
            icon("more", 16),
          ),
        ),
      ),
    ]),
  );
}

function conversationMenu(anchor, conv) {
  menu(anchor, [
    {
      icon: "edit",
      label: "重命名",
      onClick: async () => {
        const title = await promptDialog({ title: "重命名对话", label: "标题", value: conv.title });
        if (!title) return;
        await api(`/api/conversations/${conv.id}`, { method: "PATCH", body: { title } });
        loadConversations();
      },
    },
    {
      icon: "pin",
      label: conv.pinned ? "取消置顶" : "置顶",
      onClick: async () => {
        await api(`/api/conversations/${conv.id}`, { method: "PATCH", body: { pinned: !conv.pinned } });
        loadConversations();
      },
    },
    { sep: true },
    {
      icon: "trash",
      label: "删除",
      danger: true,
      onClick: async () => {
        const ok = await confirmDialog({ title: "删除对话", message: `删除「${conv.title}」后无法恢复。`, confirmText: "删除", danger: true });
        if (!ok) return;
        await api(`/api/conversations/${conv.id}`, { method: "DELETE" });
        await loadConversations();
        if (currentPath() === `/chat/${conv.id}`) navigate("/chat");
      },
    },
  ]);
}

function userCard() {
  const user = store.user;
  const initial = (user.display_name || user.username).slice(0, 1).toUpperCase();
  const card = h(
    "button",
    { class: "user-card", type: "button" },
    h("span", { class: "user-avatar" }, initial),
    h("span", { class: "user-info" }, h("span", { class: "user-name" }, user.display_name), h("span", { class: "user-role" }, user.role === "admin" ? "管理员" : "成员")),
    icon("chevronDown", 16, "muted"),
  );
  card.addEventListener("click", () => {
    const pref = themePref();
    menu(card, [
      { header: true, label: "外观" },
      { icon: "sun", label: "浅色", active: pref === "light", onClick: () => applyTheme("light") },
      { icon: "moon", label: "深色", active: pref === "dark", onClick: () => applyTheme("dark") },
      { icon: "monitor", label: "跟随系统", active: pref === "system", onClick: () => applyTheme("system") },
      { sep: true },
      { icon: "user", label: "个人设置", onClick: () => navigate("/settings/profile") },
      { icon: "key", label: "API 与集成", onClick: () => navigate("/settings/integrations") },
      { sep: true },
      {
        icon: "logout",
        label: "退出登录",
        onClick: async () => {
          await api("/api/auth/logout", { method: "POST" });
          store.user = null;
          location.hash = "#/chat";
          boot();
        },
      },
    ]);
  });
  return card;
}

document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k" && store.user) {
    e.preventDefault();
    navigate("/chat");
  }
});

boot();
