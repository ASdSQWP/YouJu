import { ICONS, LOGO } from "./icons.js?v=20261016h";

export function h(tag, props, ...children) {
  const el = document.createElement(tag);
  if (props) {
    for (const [key, value] of Object.entries(props)) {
      if (value === undefined || value === null || value === false) continue;
      if (key === "class") el.className = value;
      else if (key === "style" && typeof value === "object") Object.assign(el.style, value);
      else if (key === "html") el.innerHTML = value;
      else if (key === "dataset") Object.assign(el.dataset, value);
      else if (key.startsWith("on") && typeof value === "function") el.addEventListener(key.slice(2), value);
      else if (key === "value" || key === "checked") el[key] = value;
      else if (value === true) el.setAttribute(key, "");
      else el.setAttribute(key, String(value));
    }
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

// 原生 replaceChildren 会把 null 当成文字 "null" 插进去，这里先过滤掉
export function fill(el, ...children) {
  el.replaceChildren(...children.flat(Infinity).filter((c) => c !== null && c !== undefined && c !== false));
  return el;
}

export function icon(name, size = 18, cls = "") {
  const span = document.createElement("span");
  span.className = `icon ${cls}`.trim();
  span.innerHTML = `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name] || ""}</svg>`;
  return span;
}

export function logo(size = 28) {
  return h("span", { class: "logo", style: { width: `${size}px`, height: `${size}px` }, html: LOGO });
}

export function spinner(size = 14) {
  return h("span", { class: "spin", style: { width: `${size}px`, height: `${size}px` } });
}

let toastHost = null;
export function toast(message, kind = "info", ms = 2800) {
  if (!toastHost) toastHost = document.body.appendChild(h("div", { class: "toasts" }));
  const name = kind === "error" ? "alert" : kind === "ok" ? "check" : "info";
  const el = h("div", { class: `toast toast-${kind}` }, icon(name, 16), h("span", null, message));
  toastHost.append(el);
  setTimeout(() => {
    el.classList.add("leaving");
    setTimeout(() => el.remove(), 220);
  }, ms);
}

export function openImageLightbox(src, alt = "预览") {
  if (!src) return;
  const backdrop = h("div", { class: "img-lightbox", role: "dialog", "aria-modal": "true", "aria-label": alt });
  let closed = false;
  const onKey = (e) => {
    if (e.key === "Escape") close();
  };
  function close() {
    if (closed) return;
    closed = true;
    document.removeEventListener("keydown", onKey);
    backdrop.classList.add("leaving");
    setTimeout(() => backdrop.remove(), 160);
  }
  const img = h("img", { src, alt, class: "img-lightbox-pic", draggable: "false" });
  fill(
    backdrop,
    h("button", { class: "img-lightbox-close icon-btn", type: "button", title: "关闭", onclick: close }, icon("x")),
    img,
  );
  backdrop.addEventListener("click", (e) => {
    if (e.target === backdrop || e.target === img) close();
  });
  document.addEventListener("keydown", onKey);
  document.body.append(backdrop);
  return { close };
}

export function modal({ title, body, actions = [], width = 520, onClose } = {}) {
  const backdrop = h("div", { class: "modal-backdrop" });
  const box = h("div", { class: "modal", role: "dialog", "aria-modal": "true", style: { width: `min(${width}px, calc(100vw - 24px))` } });
  let closed = false;
  const onKey = (e) => {
    if (e.key === "Escape") close();
  };
  function close() {
    if (closed) return;
    closed = true;
    document.removeEventListener("keydown", onKey);
    backdrop.classList.add("leaving");
    setTimeout(() => backdrop.remove(), 140);
    onClose?.();
  }
  const foot = actions.length
    ? h(
        "div",
        { class: "modal-foot" },
        ...actions.map((action) =>
          h(
            "button",
            {
              class: `btn ${action.kind ? `btn-${action.kind}` : ""}`,
              type: "button",
              onclick: async (e) => {
                if (!action.onClick) return close();
                const btn = e.currentTarget;
                btn.disabled = true;
                try {
                  const keep = await action.onClick();
                  if (keep !== false) close();
                } catch (err) {
                  toast(err.message || String(err), "error");
                } finally {
                  btn.disabled = false;
                }
              },
            },
            action.label,
          ),
        ),
      )
    : null;
  fill(
    box,
    h("div", { class: "modal-head" }, h("div", { class: "modal-title" }, title), h("button", { class: "icon-btn", type: "button", title: "关闭", onclick: close }, icon("x"))),
    h("div", { class: "modal-body" }, body),
    foot,
  );
  backdrop.append(box);
  backdrop.addEventListener("mousedown", (e) => {
    if (e.target === backdrop) close();
  });
  document.addEventListener("keydown", onKey);
  document.body.append(backdrop);
  setTimeout(() => box.querySelector(".modal-body input:not([type=checkbox]), .modal-body textarea")?.focus(), 40);
  return { close, box };
}

export function confirmDialog({ title = "确认操作", message, confirmText = "确定", danger = false }) {
  return new Promise((resolve) => {
    let answered = false;
    modal({
      title,
      width: 420,
      body: h("p", { class: "confirm-text" }, message),
      onClose: () => {
        if (!answered) resolve(false);
      },
      actions: [
        { label: "取消" },
        {
          label: confirmText,
          kind: danger ? "danger-solid" : "primary",
          onClick: () => {
            answered = true;
            resolve(true);
          },
        },
      ],
    });
  });
}

export function promptDialog({ title, label, value = "", placeholder = "", confirmText = "保存" }) {
  return new Promise((resolve) => {
    let answered = false;
    const input = h("input", { class: "input", value, placeholder });
    const dialog = modal({
      title,
      width: 440,
      body: field(label, input),
      onClose: () => {
        if (!answered) resolve(null);
      },
      actions: [
        { label: "取消" },
        {
          label: confirmText,
          kind: "primary",
          onClick: () => {
            answered = true;
            resolve(input.value.trim());
          },
        },
      ],
    });
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.isComposing) {
        answered = true;
        resolve(input.value.trim());
        dialog.close();
      }
    });
  });
}

let openMenu = null;
function onOutside(e) {
  if (openMenu && !openMenu.contains(e.target)) closeMenu();
}
function onMenuKey(e) {
  if (e.key === "Escape") closeMenu();
}
export function closeMenu() {
  openMenu?.remove();
  openMenu = null;
  document.removeEventListener("mousedown", onOutside, true);
  document.removeEventListener("keydown", onMenuKey);
}

export function menu(anchor, items, { align = "left", width } = {}) {
  closeMenu();
  const el = h("div", { class: "menu", role: "menu" });
  for (const item of items) {
    if (!item) continue;
    if (item.sep) {
      el.append(h("div", { class: "menu-sep" }));
      continue;
    }
    if (item.header) {
      el.append(h("div", { class: "menu-label" }, item.label));
      continue;
    }
    el.append(
      h(
        "button",
        {
          class: `menu-item${item.danger ? " danger" : ""}${item.active ? " active" : ""}`,
          type: "button",
          onclick: () => {
            closeMenu();
            item.onClick?.();
          },
        },
        item.lead || (item.icon ? icon(item.icon, 16) : null),
        h("span", { class: "menu-text" }, h("span", { class: "menu-main" }, item.label), item.sub ? h("span", { class: "menu-sub" }, item.sub) : null),
        item.active ? icon("check", 16, "menu-check") : null,
      ),
    );
  }
  if (width) el.style.minWidth = `${width}px`;
  document.body.append(el);
  const rect = anchor.getBoundingClientRect();
  const mw = el.offsetWidth;
  const mh = el.offsetHeight;
  let left = align === "right" ? rect.right - mw : rect.left;
  left = Math.max(8, Math.min(left, window.innerWidth - mw - 8));
  let top = rect.bottom + 6;
  if (top + mh > window.innerHeight - 8) top = Math.max(8, rect.top - mh - 6);
  el.style.left = `${left}px`;
  el.style.top = `${top}px`;
  openMenu = el;
  document.addEventListener("mousedown", onOutside, true);
  document.addEventListener("keydown", onMenuKey);
  return el;
}

export function field(label, control, hint) {
  return h("div", { class: "field" }, label ? h("label", { class: "field-label" }, label) : null, control, hint ? h("div", { class: "hint" }, hint) : null);
}

export function switchInput(checked, onchange, label) {
  const input = h("input", { type: "checkbox", checked, onchange: (e) => onchange?.(e.target.checked) });
  return h("label", { class: "switch-row" }, h("span", { class: "switch" }, input, h("span", { class: "slider" })), label ? h("span", { class: "switch-label" }, label) : null);
}

export function segmented(options, value, onchange) {
  const wrap = h("div", { class: "seg" });
  const render = (current) => {
    wrap.replaceChildren(
      ...options.map(([key, label]) =>
        h(
          "button",
          {
            type: "button",
            class: key === current ? "active" : "",
            onclick: () => {
              render(key);
              onchange(key);
            },
          },
          label,
        ),
      ),
    );
  };
  render(value);
  return wrap;
}

export function emptyState(iconName, title, desc, action) {
  return h("div", { class: "empty" }, h("div", { class: "empty-icon" }, icon(iconName, 24)), h("h3", null, title), desc ? h("p", null, desc) : null, action || null);
}

export function pageShell({ title, desc, actions = [] }, ...children) {
  return h(
    "div",
    { class: "page" },
    h(
      "div",
      { class: "page-inner" },
      h(
        "div",
        { class: "page-head" },
        h("div", { class: "page-title" }, h("button", { class: "icon-btn only-mobile", type: "button", onclick: openSidebar, title: "菜单" }, icon("menu")), h("div", null, h("h1", null, title), desc ? h("p", null, desc) : null)),
        actions.length ? h("div", { class: "page-actions" }, ...actions) : null,
      ),
      ...children,
    ),
  );
}

export function openSidebar() {
  document.querySelector(".app")?.classList.add("sb-open");
}

export function closeSidebar() {
  document.querySelector(".app")?.classList.remove("sb-open");
}

export function softColor(hex, alpha = 0.14) {
  const value = String(hex || "#0f766e").replace("#", "");
  const n = parseInt(value.length === 3 ? value.replace(/./g, "$&$&") : value, 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
}

export function badge(text, kind = "") {
  return h("span", { class: `badge${kind ? ` badge-${kind}` : ""}` }, text);
}

export async function copyText(text, note = "已复制") {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const area = h("textarea", { style: { position: "fixed", opacity: "0" } });
    area.value = text;
    document.body.append(area);
    area.select();
    document.execCommand("copy");
    area.remove();
  }
  toast(note, "ok", 1400);
}

const pad = (n) => String(n).padStart(2, "0");

export function fmtDate(iso, withTime = false) {
  if (!iso) return "";
  const d = new Date(iso);
  const day = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  return withTime ? `${day} ${pad(d.getHours())}:${pad(d.getMinutes())}` : day;
}

export function timeAgo(iso) {
  if (!iso) return "";
  const seconds = (Date.now() - new Date(iso).getTime()) / 1000;
  if (seconds < 60) return "刚刚";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
  if (seconds < 86400 * 7) return `${Math.floor(seconds / 86400)} 天前`;
  return fmtDate(iso);
}

export function fmtBytes(n) {
  if (!n) return "0 B";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

export function fmtNum(n) {
  return Number(n || 0).toLocaleString("zh-CN");
}

export function fmtMs(ms) {
  if (ms === null || ms === undefined) return "";
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`;
}

export function themePref() {
  const saved = localStorage.getItem("yj-theme");
  return saved === "light" || saved === "dark" || saved === "system" ? saved : "system";
}

function osTheme() {
  const marked = document.documentElement.dataset.osTheme;
  if (marked === "light" || marked === "dark") return marked;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export function applyTheme(pref = themePref()) {
  if (pref !== "light" && pref !== "dark" && pref !== "system") pref = "system";
  localStorage.setItem("yj-theme", pref);
  document.cookie = `yj-theme=${pref}; Path=/; Max-Age=31536000; SameSite=Lax`;
  const theme = pref === "system" ? osTheme() : pref;
  const root = document.documentElement;
  root.dataset.theme = theme;
  root.dataset.themePref = pref;
  root.style.colorScheme = `only ${theme}`;
  root.style.backgroundColor = theme === "dark" ? "#171717" : "#ffffff";
  const meta = document.querySelector('meta[name="color-scheme"]');
  if (meta) meta.setAttribute("content", theme);
}
