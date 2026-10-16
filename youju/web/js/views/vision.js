import { api } from "../api.js";
import { fill, badge, confirmDialog, emptyState, field, h, icon, modal, openImageLightbox, openSidebar, segmented, spinner, switchInput, timeAgo, toast } from "../ui.js?v=20261016f";
import { loadEventCount, store } from "../state.js";
import { navigate, replacePath } from "../router.js";
import { levelBadge } from "../components.js";

const ALG_FALLBACK = [
  ["no_helmet", "未戴安全帽"],
  ["no_belt", "安全带缺失"],
  ["no_hook", "挂钩未连接"],
  ["edge_unguarded", "临边防护缺失"],
  ["fire_smoke", "烟火"],
  ["no_supervisor", "无监护人"],
  ["limit_intrusion", "限界侵入"],
];

const VIS_LABEL = { private: "仅自己", team: "团队共享", shared: "指定用户" };
const DET_LABEL = { small: "仅小模型", vlm: "仅大模型", small_then_vlm: "小模型+大模型后处理" };

function algoLabel(id, catalog) {
  return (catalog.find((a) => a.id === id) || {}).name || ALG_FALLBACK.find((a) => a[0] === id)?.[1] || id;
}

function catLabel(id, cats) {
  return (cats || []).find((c) => c.id === id)?.name || id || "其他";
}

/** 预设 + 自定义分类选择器。getValue() 返回最终分类字符串。 */
function categoryPicker(presetCats, initial) {
  const cats = presetCats?.length ? presetCats : [{ id: "other", name: "其他" }];
  const known = new Set(cats.map((c) => c.id));
  const initialCustom = initial && !known.has(initial) ? initial : "";
  let mode = initialCustom ? "__custom__" : known.has(initial) ? initial : cats[0].id;
  const custom = h("input", {
    class: "input",
    placeholder: "输入自定义分类，例如 车间巡检",
    maxlength: 32,
    value: initialCustom,
    style: { display: mode === "__custom__" ? "" : "none", marginTop: "8px" },
  });
  const sel = h(
    "select",
    { class: "select" },
    ...cats.map((c) => h("option", { value: c.id, selected: c.id === mode }, c.name)),
    h("option", { value: "__custom__", selected: mode === "__custom__" }, "自定义…"),
  );
  sel.addEventListener("change", () => {
    mode = sel.value;
    custom.style.display = mode === "__custom__" ? "" : "none";
    if (mode === "__custom__") custom.focus();
  });
  return {
    el: h("div", { class: "vis-cat-picker" }, sel, custom),
    getValue() {
      if (sel.value === "__custom__") {
        const text = custom.value.trim().slice(0, 32);
        return text || "other";
      }
      return sel.value;
    },
  };
}

function extraCategories(sources, presetCats) {
  const known = new Set((presetCats || []).map((c) => c.id));
  const seen = new Set();
  const out = [];
  for (const src of sources || []) {
    const id = (src.category || "").trim();
    if (!id || known.has(id) || seen.has(id)) continue;
    seen.add(id);
    out.push({ id, name: id });
  }
  return out;
}

function needsVlm(detector) {
  return detector === "vlm" || detector === "small_then_vlm";
}

function goConfigureVlm(returnPath) {
  try {
    sessionStorage.setItem("youju_return", returnPath || "/vision");
  } catch {
    /* ignore */
  }
  navigate("/settings/models");
}

export function visionView({ params, path }) {
  const st = {
    tab: path.startsWith("/vision/rules") ? "rules" : path.startsWith("/vision/media") ? "media" : "wall",
    selected: params.id && params.id !== "rules" && params.id !== "media" ? Number(params.id) : null,
    sources: [],
    groups: [],
    rules: [],
    alerts: [],
    directory: [],
    catalog: { algorithms: [], detectors: [], categories: [] },
    vlm: { ready: false, hint: "" },
    preview: null,
    timer: null,
    playlistTimer: null,
    mediaFilter: "all",
    collapsedOwners: {},
    folders: [],
    picked: [],
    uploadFolder: "",
    queue: [],
    autoplay: true,
    alertScope: "current",
  };

  function isImageSrc(src) {
    return src?.preview_kind === "image";
  }

  function queueList() {
    return st.queue.map((id) => st.sources.find((s) => s.id === id)).filter(Boolean);
  }

  function toggleQueue(id, force) {
    const i = st.queue.indexOf(id);
    if (force === true && i < 0) st.queue.push(id);
    else if (force === false && i >= 0) st.queue.splice(i, 1);
    else if (i >= 0) st.queue.splice(i, 1);
    else st.queue.push(id);
  }

  function selectSource(id) {
    st.selected = id;
    replacePath(`/vision/${id}`);
  }
  const body = h("div", { class: "vis-body" });
  const tabs = h("div", { class: "ev-tabs vis-tabs" });
  const el = h(
    "div",
    { class: "vision-page" },
    h(
      "div",
      { class: "page-head events-head" },
      h(
        "div",
        { class: "page-title" },
        h("button", { class: "icon-btn only-mobile", type: "button", onclick: openSidebar, title: "菜单" }, icon("menu")),
        h("div", null, h("h1", null, "视觉检测"), h("p", null, "媒体库管理图片与视频，监视墙点选播放并选算法检测。大模型为可选项。")),
      ),
      h("div", { class: "page-actions vis-pull" }, pullBar()),
    ),
    tabs,
    body,
  );

  function pullBar() {
    const input = h("input", { class: "input", placeholder: "输入视频名称，例如 杆塔3", maxlength: 80 });
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") doPull(input.value);
    });
    return h(
      "div",
      { class: "vis-pull-row" },
      input,
      h("button", { class: "btn btn-primary", type: "button", onclick: () => doPull(input.value) }, icon("play", 15), "拉流"),
    );
  }

  async function doPull(name) {
    const text = (name || "").trim();
    if (!text) {
      toast("请输入视频名称", "error");
      return;
    }
    try {
      const src = await api("/api/vision/pull", { method: "POST", body: { name: text } });
      toast(`已拉流「${src.name}」`, "ok");
      st.tab = "wall";
      st.selected = src.id;
      replacePath(`/vision/${src.id}`);
      await reload();
    } catch (err) {
      toast(err.message, "error");
    }
  }

  function renderTabs() {
    fill(
      tabs,
      h("button", { class: `ev-tab${st.tab === "media" ? " active" : ""}`, type: "button", onclick: () => switchTab("media") }, icon("file", 14), "媒体库"),
      h("button", { class: `ev-tab${st.tab === "wall" ? " active" : ""}`, type: "button", onclick: () => switchTab("wall") }, icon("video", 14), "监视墙"),
      h("button", { class: `ev-tab${st.tab === "rules" ? " active" : ""}`, type: "button", onclick: () => switchTab("rules") }, icon("crosshair", 14), "检测规则"),
    );
  }

  function switchTab(tab) {
    st.tab = tab;
    if (tab !== "wall") {
      clearTimeout(st.timer);
      clearTimeout(st.playlistTimer);
    }
    const pathMap = {
      media: "/vision/media",
      rules: "/vision/rules",
      wall: st.selected ? `/vision/${st.selected}` : "/vision",
    };
    replacePath(pathMap[tab]);
    renderTabs();
    render();
  }

  async function reload() {
    try {
      const [catalog, sourcesRes, rules, alerts, vlm, directory] = await Promise.all([
        api("/api/vision/catalog"),
        api("/api/vision/sources?group=owner"),
        api("/api/vision/rules"),
        api("/api/vision/alerts"),
        api("/api/vision/vlm-status"),
        api("/api/users/directory").catch(() => ({ users: [] })),
      ]);
      st.catalog = catalog;
      st.sources = sourcesRes.sources || [];
      st.groups = sourcesRes.groups || [];
      st.folders = sourcesRes.folders || [];
      const aliveIds = new Set(st.sources.map((s) => s.id));
      st.picked = st.picked.filter((id) => aliveIds.has(id));
      st.rules = rules.rules;
      st.alerts = alerts.alerts;
      st.vlm = vlm;
      st.directory = directory.users || [];
      const alive = new Set(st.sources.map((s) => s.id));
      st.queue = st.queue.filter((id) => alive.has(id));
      if (!st.selected && st.sources.length) st.selected = st.sources[0].id;
      renderTabs();
      render();
    } catch (err) {
      fill(body, h("div", { class: "notice notice-danger" }, err.message));
    }
  }

  function render() {
    if (st.tab === "rules") renderRules();
    else if (st.tab === "media") renderMedia();
    else renderWall();
  }

  function ruleForSource(srcId) {
    const bound = st.rules.filter((r) => r.source_id === srcId && r.enabled);
    return bound[0] || null;
  }

  /* ---------- 媒体库 ---------- */

  function renderMedia() {
    const panel = h("div", { class: "vis-media-page" });
    fill(body, panel);
    fill(panel, uploadCard(), mediaToolbar(), filterBar(), mediaGroups());
  }

  function mediaToolbar() {
    const n = st.picked.length;
    const moveSel = h(
      "select",
      { class: "select select-sm" },
      h("option", { value: "" }, "移到未分组"),
      ...st.folders.map((f) => h("option", { value: String(f.id) }, f.name)),
    );
    return h(
      "div",
      { class: "vis-media-toolbar" },
      h(
        "button",
        {
          class: "btn btn-sm",
          type: "button",
          onclick: async () => {
            const name = window.prompt("文件夹名称", "未命名文件夹");
            if (!name || !name.trim()) return;
            try {
              const folder = await api("/api/vision/folders", { method: "POST", body: { name: name.trim() } });
              st.uploadFolder = String(folder.id);
              toast(`已创建文件夹「${folder.name}」`, "ok");
              await reload();
            } catch (err) {
              toast(err.message, "error");
            }
          },
        },
        icon("plus", 14),
        "新建文件夹",
      ),
      h("span", { class: "muted small" }, n ? `已选 ${n} 项` : "勾选媒体后可批量删除或移入文件夹"),
      h(
        "button",
        {
          class: "btn btn-sm",
          type: "button",
          disabled: !n || undefined,
          onclick: async () => {
            try {
              await api("/api/vision/sources/move", {
                method: "POST",
                body: { ids: st.picked, folder_id: moveSel.value ? Number(moveSel.value) : null },
              });
              toast("已移动", "ok");
              await reload();
            } catch (err) {
              toast(err.message, "error");
            }
          },
        },
        "移入",
      ),
      moveSel,
      h(
        "button",
        {
          class: "btn btn-sm btn-danger",
          type: "button",
          disabled: !n || undefined,
          onclick: async () => {
            const ok = await confirmDialog({ title: "删除所选媒体", message: `删除选中的 ${n} 个媒体？`, confirmText: "删除", danger: true });
            if (!ok) return;
            try {
              await api("/api/vision/sources/batch-delete", { method: "POST", body: { ids: st.picked } });
              st.picked = [];
              toast("已删除", "ok");
              await reload();
            } catch (err) {
              toast(err.message, "error");
            }
          },
        },
        "删除所选",
      ),
    );
  }

  function uploadCard() {
    const cats = st.catalog.categories?.length ? st.catalog.categories : [{ id: "other", name: "其他" }];
    let visibility = "private";
    const shared = new Set();
    const cat = categoryPicker(cats);
    const shareBox = h("div", { class: "vis-share-box muted small" }, "选择「指定用户」后在此勾选成员");
    const visSeg = segmented(
      [
        ["private", "仅自己"],
        ["team", "团队共享"],
        ["shared", "指定用户"],
      ],
      visibility,
      (v) => {
        visibility = v;
        paintShare();
      },
    );
    function paintShare() {
      if (visibility !== "shared") {
        fill(shareBox, h("span", { class: "muted small" }, "仅「指定用户」需要选择共享对象"));
        return;
      }
      fill(
        shareBox,
        h(
          "div",
          { class: "vis-share-list" },
          ...st.directory.map((u) => {
            const box = h("input", { type: "checkbox", checked: shared.has(u.id) });
            box.addEventListener("change", () => {
              if (box.checked) shared.add(u.id);
              else shared.delete(u.id);
            });
            return h("label", { class: "check-item compact" }, box, `${u.display_name || u.username}`);
          }),
        ),
      );
    }
    paintShare();
    const folderSel = h(
      "select",
      { class: "select", onchange: (e) => (st.uploadFolder = e.target.value) },
      h("option", { value: "", selected: !st.uploadFolder }, "不放入文件夹"),
      ...st.folders.map((f) => h("option", { value: String(f.id), selected: String(f.id) === String(st.uploadFolder) }, f.name)),
    );
    const input = h("input", { type: "file", accept: "image/*,video/mp4,video/webm", multiple: true, hidden: true });
    input.addEventListener("change", async () => {
      if (!input.files?.length) return;
      const form = new FormData();
      for (const file of input.files) form.append("files", file);
      form.append("category", cat.getValue());
      form.append("visibility", visibility);
      form.append("shared_user_ids", JSON.stringify([...shared]));
      if (folderSel.value) form.append("folder_id", folderSel.value);
      try {
        const res = await api("/api/vision/sources/upload", { method: "POST", form });
        if (res.created?.length) {
          toast(`已上传 ${res.created.length} 个媒体`, "ok");
          st.selected = res.created[0].id;
        }
        if (res.skipped?.length) toast(res.skipped[0].reason, "error");
        await reload();
      } catch (err) {
        toast(err.message, "error");
      }
      input.value = "";
    });
    return h(
      "section",
      { class: "card vis-media-upload" },
      h("div", { class: "section-title" }, icon("upload", 16), "上传图片 / 视频"),
      h("p", { class: "muted", style: { marginTop: 0 } }, "支持图片与 mp4 / webm，单个文件不超过 500 MB。上传后可在监视墙点选播放检测。"),
      h("div", { class: "row-2" }, field("分类", cat.el, "可选预设，或选「自定义…」自行填写"), field("可见性", visSeg)),
      field("放入文件夹", folderSel, "文件夹里可以同时放图片和视频。"),
      shareBox,
      h("div", { class: "form-actions", style: { justifyContent: "flex-start" } }, h("button", { class: "btn btn-primary", type: "button", onclick: () => input.click() }, input, icon("upload", 14), "选择文件上传")),
    );
  }

  function filterBar() {
    const presets = st.catalog.categories || [];
    const cats = [{ id: "all", name: "全部分类" }, ...presets, ...extraCategories(st.sources, presets)];
    return h(
      "div",
      { class: "vis-media-filter" },
      ...cats.map((c) =>
        h(
          "button",
          {
            class: `btn btn-sm${st.mediaFilter === c.id ? " btn-primary" : ""}`,
            type: "button",
            onclick: () => {
              st.mediaFilter = c.id;
              render();
            },
          },
          c.name,
        ),
      ),
    );
  }

  function mediaGroups() {
    const wrap = h("div", { class: "vis-media-groups" });
    if (!st.groups.length) {
      fill(wrap, emptyState("file", "媒体库还是空的", "上传图片或视频后会出现在这里，可按上传者分组管理。"));
      return wrap;
    }
    fill(
      wrap,
      ...st.groups.map((g) => {
        const items = (g.sources || []).filter((s) => st.mediaFilter === "all" || s.category === st.mediaFilter);
        const ownFolders = st.folders.filter((f) => f.owner_id === g.owner_id);
        if (!items.length && !ownFolders.length) return null;
        const key = String(g.owner_id);
        const open = st.collapsedOwners[key] !== false;
        const list = h("div", { class: "vis-media-list" });
        const buckets = new Map();
        for (const src of items) {
          const fid = src.folder_id || 0;
          if (!buckets.has(fid)) buckets.set(fid, []);
          buckets.get(fid).push(src);
        }
        const folderOrder = [
          ...st.folders.filter((f) => f.owner_id === g.owner_id).map((f) => f.id),
          ...[...buckets.keys()].filter((id) => id && !st.folders.some((f) => f.id === id)),
          0,
        ];
        const mediaRow = (src) =>
              h(
                "div",
                { class: `vis-media-row${st.picked.includes(src.id) ? " picked" : ""}` },
                src.can_edit
                  ? h("input", {
                      type: "checkbox",
                      class: "vis-queue-check",
                      checked: st.picked.includes(src.id),
                      onclick: (e) => e.stopPropagation(),
                      onchange: (e) => {
                        if (e.target.checked) st.picked.push(src.id);
                        else st.picked = st.picked.filter((id) => id !== src.id);
                        render();
                      },
                    })
                  : h("span", { class: "vis-queue-spacer" }),
                h(
                  "div",
                  { class: "vis-media-thumb" },
                  src.preview_kind === "video" && src.media_url
                    ? h("video", { src: src.media_url, muted: true })
                    : src.media_url
                      ? h("img", { src: src.media_url, alt: src.name })
                      : h("div", { class: "vis-media-placeholder" }, icon("video", 20)),
                ),
                h(
                  "div",
                  { class: "grow" },
                  h("b", null, src.name),
                  h(
                    "div",
                    { class: "muted small" },
                    catLabel(src.category, st.catalog.categories),
                    " · ",
                    VIS_LABEL[src.visibility] || src.visibility,
                    " · ",
                    src.kind === "file" ? "上传" : "演示",
                  ),
                ),
                h(
                  "div",
                  { class: "vis-media-actions" },
                  h(
                    "button",
                    {
                      class: "btn btn-sm btn-primary",
                      type: "button",
                      onclick: () => {
                        st.selected = src.id;
                        switchTab("wall");
                      },
                    },
                    "上墙检测",
                  ),
                  src.can_edit
                    ? h("button", { class: "btn btn-sm", type: "button", onclick: () => editMedia(src) }, "共享")
                    : null,
                  src.can_edit && src.kind === "file"
                    ? h(
                        "button",
                        {
                          class: "btn btn-sm btn-danger",
                          type: "button",
                          onclick: async () => {
                            const ok = await confirmDialog({ title: "删除媒体", message: `删除「${src.name}」？`, confirmText: "删除", danger: true });
                            if (!ok) return;
                            await api(`/api/vision/sources/${src.id}`, { method: "DELETE" });
                            if (st.selected === src.id) st.selected = null;
                            reload();
                          },
                        },
                        "删除",
                      )
                    : null,
                ),
              );
        if (open) {
          const blocks = [];
          const seen = new Set();
          for (const fid of folderOrder) {
            if (seen.has(fid)) continue;
            seen.add(fid);
            const rows = buckets.get(fid) || [];
            const folder = st.folders.find((f) => f.id === fid);
            if (!rows.length && !folder) continue;
            if (fid === 0 && !rows.length) continue;
            blocks.push(
              h(
                "div",
                { class: "vis-folder" },
                h(
                  "div",
                  { class: "vis-folder-head" },
                  icon("file", 14),
                  h("b", null, folder ? folder.name : "未分组"),
                  h("span", { class: "muted small" }, `${rows.length} 个`),
                  folder
                    ? h(
                        "button",
                        {
                          class: "btn btn-sm",
                          type: "button",
                          onclick: async () => {
                            const ok = await confirmDialog({ title: "删除文件夹", message: `删除文件夹「${folder.name}」？里面的媒体会回到未分组。`, confirmText: "删除", danger: true });
                            if (!ok) return;
                            await api(`/api/vision/folders/${folder.id}`, { method: "DELETE" });
                            reload();
                          },
                        },
                        "删除文件夹",
                      )
                    : null,
                ),
                ...rows.map(mediaRow),
              ),
            );
          }
          fill(list, ...blocks);
        }
        return h(
          "section",
          { class: "card vis-media-group" },
          h(
            "button",
            {
              class: "vis-group-head",
              type: "button",
              onclick: () => {
                st.collapsedOwners[key] = !open;
                render();
              },
            },
            icon(open ? "chevronDown" : "chevronRight", 14),
            h("b", null, g.owner_name || `用户 ${g.owner_id}`),
            h("span", { class: "muted small" }, `${items.length} 个`),
          ),
          open ? list : null,
        );
      }).filter(Boolean),
    );
    return wrap;
  }

  function editMedia(src) {
    let visibility = src.visibility || "private";
    const shared = new Set(src.shared_user_ids || []);
    const cats = st.catalog.categories?.length ? st.catalog.categories : [{ id: "other", name: "其他" }];
    const cat = categoryPicker(cats, src.category);
    const name = h("input", { class: "input", value: src.name, maxlength: 80 });
    const shareBox = h("div");
    const paint = () => {
      if (visibility !== "shared") {
        fill(shareBox, h("p", { class: "hint" }, "团队共享对所有登录成员可见；仅自己只有你能看见。"));
        return;
      }
      fill(
        shareBox,
        h(
          "div",
          { class: "vis-share-list" },
          ...st.directory.map((u) => {
            const box = h("input", { type: "checkbox", checked: shared.has(u.id) });
            box.addEventListener("change", () => {
              if (box.checked) shared.add(u.id);
              else shared.delete(u.id);
            });
            return h("label", { class: "check-item compact" }, box, u.display_name || u.username);
          }),
        ),
      );
    };
    paint();
    modal({
      title: "编辑媒体",
      body: h(
        "div",
        { class: "form" },
        field("名称", name),
        field("分类", cat.el, "可选预设，或选「自定义…」自行填写"),
        field(
          "可见性",
          segmented(
            [
              ["private", "仅自己"],
              ["team", "团队共享"],
              ["shared", "指定用户"],
            ],
            visibility,
            (v) => {
              visibility = v;
              paint();
            },
          ),
        ),
        shareBox,
      ),
      actions: [
        { label: "取消" },
        {
          label: "保存",
          kind: "primary",
          onClick: async () => {
            await api(`/api/vision/sources/${src.id}`, {
              method: "PATCH",
              body: {
                name: name.value.trim() || src.name,
                category: cat.getValue(),
                visibility,
                shared_user_ids: [...shared],
              },
            });
            toast("已保存", "ok");
            reload();
          },
        },
      ],
    });
  }

  /* ---------- 监视墙 ---------- */

  function renderWall() {
    const list = h("div", { class: "vis-src-list" });
    const stage = h("div", { class: "vis-stage-wrap" });
    const side = h("div", { class: "vis-alerts" });
    const alertHead = h("div", { class: "vis-col-head vis-alert-head" });
    fill(
      body,
      h(
        "div",
        { class: "vis-layout" },
        h(
          "div",
          { class: "vis-col vis-col-src" },
          h(
            "div",
            { class: "vis-col-head" },
            h("span", null, "可见媒体"),
            h("button", { class: "btn btn-sm", type: "button", onclick: () => switchTab("media") }, icon("file", 14), "媒体库"),
          ),
          list,
        ),
        stage,
        h("div", { class: "vis-col vis-col-alert" }, alertHead, side),
      ),
    );
    renderSources(list);
    renderStage(stage);
    renderAlertHead(alertHead);
    renderAlerts(side);
    schedule();
    schedulePlaylist();
  }

  function renderSources(list) {
    if (!st.sources.length) {
      fill(list, h("div", { class: "vis-empty" }, "还没有可见媒体，去媒体库上传"));
      return;
    }
    const qset = new Set(st.queue);
    fill(
      list,
      st.queue.length
        ? h(
            "div",
            { class: "vis-queue-bar" },
            h("span", null, `检测队列 ${st.queue.length}`),
            h(
              "button",
              {
                class: "btn btn-sm",
                type: "button",
                onclick: () => {
                  st.queue = [];
                  render();
                },
              },
              "清空",
            ),
          )
        : null,
      ...st.sources.map((src) => {
        const inQ = qset.has(src.id);
        const row = h(
          "div",
          { class: `vis-src${src.id === st.selected ? " active" : ""}${inQ ? " queued" : ""}` },
          isImageSrc(src)
            ? h("input", {
                type: "checkbox",
                class: "vis-queue-check",
                title: "加入图片检测队列",
                checked: inQ,
                onclick: (e) => e.stopPropagation(),
                onchange: (e) => {
                  toggleQueue(src.id, e.target.checked);
                  render();
                },
              })
            : h("span", { class: "vis-queue-spacer" }),
          h(
            "button",
            {
              class: "vis-src-btn",
              type: "button",
              onclick: () => {
                selectSource(src.id);
                render();
              },
            },
            h("div", { class: "vis-src-top" }, h("span", { class: `vis-dot${src.live ? " on" : ""}` }), h("b", null, src.name), src.live ? badge("LIVE", "danger") : badge(isImageSrc(src) ? "图片" : src.kind === "file" ? "视频" : "演示", "plain")),
            h("div", { class: "vis-src-sub" }, catLabel(src.category, st.catalog.categories), " · ", src.owner_name || src.code || src.filename || "未命名"),
          ),
        );
        return row;
      }),
    );
  }

  function renderPlaylistBar(src) {
    const q = queueList().filter(isImageSrc);
    if (q.length < 2 && !(st.queue.length && isImageSrc(src))) return null;
    const ids = q.length ? q.map((s) => s.id) : st.queue;
    const idx = Math.max(0, ids.indexOf(src.id));
    const go = (delta) => {
      if (!ids.length) return;
      const next = ids[(idx + delta + ids.length) % ids.length];
      selectSource(next);
      render();
    };
    return h(
      "div",
      { class: "vis-playlist" },
      h("button", { class: "btn btn-sm", type: "button", title: "上一张", onclick: () => go(-1) }, icon("chevronLeft", 14)),
      h("span", { class: "vis-playlist-pos" }, `${idx + 1} / ${ids.length}`),
      h("button", { class: "btn btn-sm", type: "button", title: "下一张", onclick: () => go(1) }, icon("chevronRight", 14)),
      h(
        "button",
        {
          class: `btn btn-sm${st.autoplay ? " btn-primary" : ""}`,
          type: "button",
          onclick: () => {
            st.autoplay = !st.autoplay;
            schedulePlaylist();
            render();
          },
        },
        st.autoplay ? "自动轮播中" : "自动轮播",
      ),
      h(
        "button",
        {
          class: "btn btn-sm btn-primary",
          type: "button",
          onclick: () => startQueue(),
        },
        icon("play", 14),
        "检测队列",
      ),
      h(
        "button",
        {
          class: "btn btn-sm",
          type: "button",
          onclick: () => stopQueue(),
        },
        icon("stop", 14),
        "停队列",
      ),
    );
  }

  function renderStage(stage) {
    const src = st.sources.find((s) => s.id === st.selected);
    if (!src) {
      fill(stage, emptyState("camera", "选择左侧媒体开始监视", "图片可勾选加入检测队列轮播；也可在右上角拉流或去媒体库上传。"));
      return;
    }
    const media = h("div", { class: "vis-media" });
    const overlay = h("div", { class: "vis-overlay" });
    const hud = h("div", { class: "vis-hud" });
    const controls = h("div", { class: "vis-wall-controls" });
    const playlist = renderPlaylistBar(src);
    fill(
      stage,
      playlist,
      h(
        "div",
        { class: "vis-stage" },
        media,
        overlay,
        hud,
        h(
          "div",
          { class: "vis-stage-actions" },
          src.live
            ? h("button", { class: "btn btn-sm", type: "button", onclick: () => stopSrc(src) }, icon("stop", 14), "停止")
            : h("button", { class: "btn btn-sm btn-primary", type: "button", onclick: () => startSrc(src) }, icon("play", 14), "开始检测"),
        ),
      ),
      controls,
      src.vlm_hint
        ? h("p", { class: "hint vis-caption" }, src.vlm_hint)
        : h("p", { class: "hint vis-caption" }, "在下方选择算法与检测模式后再点「开始检测」。多张图片可勾选入队后点「检测队列」轮播。"),
    );
    paintMedia(media, overlay, hud, src);
    paintWallControls(controls, src);
  }

  function paintWallControls(root, src) {
    const algorithms = st.catalog.algorithms.length ? st.catalog.algorithms : ALG_FALLBACK.map(([id, name]) => ({ id, name }));
    const bound = ruleForSource(src.id);
    const selected = new Set(bound?.algorithms?.length ? bound.algorithms : algorithms.slice(0, 2).map((a) => a.id));
    let detector = bound?.detector || "small";
    let smallModel = bound?.small_model || st.catalog.defaults?.small_model || "demo";
    let vlmModel = bound?.vlm_model || st.catalog.defaults?.vlm_model || "";
    const chips = h("div", { class: "vis-algo-chips" });
    const modeRow = h("div", { class: "vis-mode-row" });
    const modelPick = h("div", { class: "vis-model-pick" });
    const vlmHint = h("div", { class: "vis-vlm-hint" });

    function paintChips() {
      fill(
        chips,
        ...algorithms.map((item) => {
          const on = selected.has(item.id);
          return h(
            "button",
            {
              class: `vis-chip${on ? " on" : ""}`,
              type: "button",
              onclick: () => {
                if (on) selected.delete(item.id);
                else selected.add(item.id);
                paintChips();
              },
            },
            item.name,
          );
        }),
      );
    }

    function paintMode() {
      fill(
        modeRow,
        field(
          "检测模式",
          segmented(
            [
              ["small", "仅小模型"],
              ["vlm", "仅大模型"],
              ["small_then_vlm", "小模型+后处理"],
            ],
            detector,
            (v) => {
              detector = v;
              paintModels();
            },
          ),
        ),
      );
      paintModels();
    }

    function paintModels() {
      const smalls = st.catalog.small_models?.length ? st.catalog.small_models : [
        { id: "demo", name: "场景演示", ready: true, hint: "按场景回放，不读真实像素。" },
        { id: "yolo", name: "YOLO", ready: false, hint: "把 .pt 放到 data/models/" },
      ];
      const visions = st.catalog.vision_models || [];
      const showSmall = detector === "small" || detector === "small_then_vlm";
      const showVlm = needsVlm(detector);
      const smallSel = h(
        "select",
        {
          class: "select",
          onchange: (e) => {
            smallModel = e.target.value;
            paintHint();
          },
        },
        ...smalls.map((m) => h("option", { value: m.id, selected: m.id === smallModel }, m.ready === false ? `${m.name}（未就绪）` : m.name)),
      );
      const vlmSel = h(
        "select",
        {
          class: "select",
          onchange: (e) => {
            vlmModel = e.target.value;
            paintHint();
          },
        },
        h("option", { value: "", selected: !vlmModel }, "默认视觉模型"),
        ...visions.map((m) => h("option", { value: m.ref, selected: m.ref === vlmModel }, `${m.label}${m.vision ? "" : "（未标视觉）"}`)),
      );
      fill(
        modelPick,
        showSmall ? field("小模型", smallSel, "YOLO 读取真实画面；类名需与算法 id 一致。权重放 data/models/*.pt。") : null,
        showVlm
          ? field(
              "大模型",
              visions.length ? vlmSel : h("span", { class: "muted small" }, "还没有接入对话模型"),
              "从已接入的模型里选一个看图模型，优先选带视觉标记的。",
            )
          : null,
      );
      paintHint();
    }

    function paintHint() {
      const smalls = st.catalog.small_models || [];
      const chosen = smalls.find((m) => m.id === smallModel);
      if (!needsVlm(detector)) {
        fill(vlmHint, h("span", { class: "muted small" }, chosen?.hint || "当前只用小模型。"));
        return;
      }
      if (st.vlm.ready) {
        fill(
          vlmHint,
          h("span", { class: "ok-text small" }, st.vlm.hint || "大模型已就绪"),
          h("span", { class: "muted small" }, "将分析真实画面；无截图依据不会告警。"),
        );
        return;
      }
      fill(
        vlmHint,
        h("span", { class: "muted small" }, st.vlm.hint || "尚未配置大模型（可选项）"),
        h(
          "button",
          {
            class: "btn btn-sm",
            type: "button",
            onclick: () => goConfigureVlm(`/vision/${src.id}`),
          },
          "去配置大模型",
        ),
      );
    }

    paintChips();
    paintMode();

    const apply = async () => {
      if (!selected.size) {
        toast("至少选择一个算法", "error");
        return;
      }
      try {
        await api(`/api/vision/sources/${src.id}/session-rule`, {
          method: "POST",
          body: {
            algorithms: [...selected],
            detector,
            small_model: smallModel || "demo",
            vlm_model: vlmModel || "",
            confidence: bound?.confidence ?? 0.55,
            agent_id: bound?.agent_id || store.agents.find((a) => a.allow_events)?.id || null,
            auto_push: true,
          },
        });
        toast("检测设置已应用", "ok");
        await reload();
      } catch (err) {
        toast(err.message, "error");
      }
    };

    fill(
      root,
      h("div", { class: "vis-control-block" }, h("div", { class: "vis-control-label" }, "检测算法"), chips),
      modeRow,
      modelPick,
      vlmHint,
      h("div", { class: "form-actions", style: { justifyContent: "flex-start" } }, h("button", { class: "btn btn-primary btn-sm", type: "button", onclick: apply }, "应用设置")),
    );
  }

  function paintMedia(media, overlay, hud, src) {
    const preview = st.preview?.id === src.id ? st.preview : src;
    const boxes = preview.boxes || src.boxes || [];
    const kind = src.preview_kind;
    if (kind === "video" && src.media_url) {
      const vid = media.querySelector("video");
      if (!vid || vid.getAttribute("src") !== src.media_url) {
        fill(media, h("video", { src: src.media_url, autoplay: true, muted: true, loop: true, playsinline: true, controls: true }));
      }
    } else if (kind === "image" && src.media_url) {
      const img = media.querySelector("img");
      if (!img || img.getAttribute("src") !== src.media_url) {
        fill(media, h("img", { src: src.media_url, alt: src.name }));
      }
    } else {
      const url = `${src.frame_url}?t=${Date.now()}`;
      const img = media.querySelector("img");
      if (img) img.src = url;
      else fill(media, h("img", { src: url, alt: src.name }));
    }
    paintBoxes(overlay, hud, src, boxes);
  }

  function paintBoxes(overlay, hud, src, boxes) {
    const preview = st.preview?.id === src.id ? st.preview : src;
    fill(
      overlay,
      ...(boxes || []).map((box) => {
        const [x, y, bw, bh] = box.box || [0, 0, 0, 0];
        return h(
          "div",
          {
            class: `vis-box${box.level === "high" ? " high" : ""}${box.demo ? " demo" : ""}`,
            style: { left: `${x * 100}%`, top: `${y * 100}%`, width: `${bw * 100}%`, height: `${bh * 100}%` },
          },
          h("span", null, `${box.demo ? "演示 · " : ""}${box.name} ${Math.round(box.confidence * 100)}%`),
        );
      }),
    );
    const analyzed = !!(preview.vlm_analyzed || src.vlm_analyzed);
    const hint = preview.vlm_hint || src.vlm_hint || "";
    const analyzing = !!(src.live && !analyzed && (hint.includes("进行中") || src.vlm_skipped));
    const demoOnly = isImageSrc(src) && !needsVlm((ruleForSource(src.id) || {}).detector || "small");
    let status;
    if (boxes?.length) status = h("span", { class: "vis-hit" }, `${boxes.length} 个目标`);
    else if (analyzing) status = h("span", { class: "muted" }, "分析中…");
    else if (analyzed) status = h("span", { class: "ok-text" }, "已分析 · 无违规");
    else if (src.live && demoOnly) status = h("span", { class: "muted" }, "演示相位（小模型不读真实像素）");
    else status = h("span", { class: "muted" }, src.live ? "等待检测结果…" : "画面中无命中规则的目标");
    fill(
      hud,
      h("span", { class: `vis-live${src.live ? " on" : ""}` }, src.live ? "LIVE" : "IDLE"),
      h("span", null, src.name),
      h("span", { class: "muted" }, src.code || ""),
      status,
    );
  }

  function alertRows() {
    if (st.alertScope === "all") return st.alerts;
    if (st.alertScope === "queue" && st.queue.length) {
      const q = new Set(st.queue);
      return st.alerts.filter((a) => q.has(a.source_id));
    }
    return st.selected ? st.alerts.filter((a) => a.source_id === st.selected) : st.alerts;
  }

  function renderAlertHead(head) {
    const scopes = [
      ["current", "当前"],
      ["queue", "队列"],
      ["all", "全部"],
    ];
    fill(
      head,
      h("span", null, "实时告警"),
      h(
        "div",
        { class: "vis-alert-scopes" },
        ...scopes.map(([key, label]) =>
          h(
            "button",
            {
              class: `btn btn-sm${st.alertScope === key ? " btn-primary" : ""}`,
              type: "button",
              onclick: () => {
                st.alertScope = key;
                const side = body.querySelector(".vis-alerts");
                const ah = body.querySelector(".vis-alert-head");
                if (ah) renderAlertHead(ah);
                if (side) renderAlerts(side);
              },
            },
            label,
          ),
        ),
      ),
    );
  }

  function renderAlerts(side) {
    const rows = alertRows();
    if (!rows.length) {
      fill(side, h("div", { class: "vis-empty" }, "开始检测后命中规则就会在这里出告警，并推到事件中心。大模型告警会附带截图与画面依据。"));
      return;
    }
    fill(
      side,
      ...rows.map((a) => {
        const reasons = (a.evidence || []).map((e) => e.reason).filter(Boolean);
        const srcName = a.source_name || st.sources.find((s) => s.id === a.source_id)?.name || "";
        return h(
          "button",
          {
            class: "vis-alert",
            type: "button",
            onclick: () => {
              if (a.source_id && a.source_id !== st.selected) {
                selectSource(a.source_id);
                render();
              }
              if (a.event_id) navigate(`/events/${a.event_id}`);
            },
          },
          h(
            "div",
            { class: "vis-alert-main" },
            a.snapshot_url
              ? h("img", {
                  class: "vis-alert-snap",
                  src: a.snapshot_url,
                  alt: "告警截图",
                  title: "点击放大",
                  onclick: (e) => {
                    e.stopPropagation();
                    openImageLightbox(a.snapshot_url, "告警截图");
                  },
                })
              : h("div", { class: "vis-alert-snap placeholder" }, icon("camera", 16)),
            h(
              "div",
              { class: "grow" },
              h("div", { class: "ev-row" }, levelBadge(a.level), badge(DET_LABEL[a.detector] || a.detector || "小模型", "plain"), h("span", { class: "muted small grow-right" }, timeAgo(a.created_at))),
              h("div", { class: "ev-title" }, a.title),
              h("div", { class: "ev-sub" }, srcName ? `${srcName} · ` : "", a.findings.join("、"), a.event_id ? h("span", { class: "ok-text" }, " 已推送") : null),
              reasons.length ? h("div", { class: "vis-alert-reason" }, reasons.slice(0, 2).join("；")) : null,
            ),
          ),
        );
      }),
    );
  }

  async function ensureSessionRule(src, template) {
    const bound = template || ruleForSource(src.id);
    if (!bound) return false;
    await api(`/api/vision/sources/${src.id}/session-rule`, {
      method: "POST",
      body: {
        algorithms: bound.algorithms,
        detector: bound.detector || "small",
        small_model: bound.small_model || "demo",
        vlm_model: bound.vlm_model || "",
        confidence: bound.confidence ?? 0.55,
        agent_id: bound.agent_id || store.agents.find((a) => a.allow_events)?.id || null,
        auto_push: true,
      },
    });
    return true;
  }

  async function startSrc(src) {
    try {
      const bound = ruleForSource(src.id);
      if (!bound) {
        toast("请先在下方选择算法并点「应用设置」", "error");
        return;
      }
      await api(`/api/vision/sources/${src.id}/start`, { method: "POST" });
      await reload();
    } catch (err) {
      toast(err.message, "error");
    }
  }

  async function stopSrc(src) {
    try {
      await api(`/api/vision/sources/${src.id}/stop`, { method: "POST" });
      await reload();
    } catch (err) {
      toast(err.message, "error");
    }
  }

  async function startQueue() {
    const ids = st.queue.filter((id) => {
      const s = st.sources.find((x) => x.id === id);
      return s && isImageSrc(s);
    });
    if (!ids.length) {
      toast("请先勾选左侧图片加入检测队列", "error");
      return;
    }
    const template = ruleForSource(st.selected) || ruleForSource(ids[0]);
    if (!template) {
      toast("请先对当前图应用检测设置，再启动队列", "error");
      return;
    }
    try {
      for (const id of ids) {
        const src = st.sources.find((s) => s.id === id);
        if (src) await ensureSessionRule(src, template);
      }
      await api("/api/vision/sources/batch-start", { method: "POST", body: { ids } });
      st.autoplay = true;
      if (!ids.includes(st.selected)) selectSource(ids[0]);
      toast(`已开始检测队列（${ids.length} 张）`, "ok");
      await reload();
      schedulePlaylist();
    } catch (err) {
      toast(err.message, "error");
    }
  }

  async function stopQueue() {
    const ids = st.queue.length ? st.queue : st.sources.filter((s) => s.live && isImageSrc(s)).map((s) => s.id);
    if (!ids.length) {
      toast("队列里没有在检的图片", "error");
      return;
    }
    try {
      await api("/api/vision/sources/batch-stop", { method: "POST", body: { ids } });
      st.autoplay = false;
      clearTimeout(st.playlistTimer);
      toast("已停止队列检测", "ok");
      await reload();
    } catch (err) {
      toast(err.message, "error");
    }
  }

  function schedulePlaylist() {
    clearTimeout(st.playlistTimer);
    if (st.tab !== "wall" || !st.autoplay) return;
    const ids = queueList().filter(isImageSrc).map((s) => s.id);
    if (ids.length < 2) return;
    const anyLive = ids.some((id) => st.sources.find((s) => s.id === id)?.live);
    if (!anyLive) return;
    st.playlistTimer = setTimeout(() => {
      if (st.tab !== "wall" || !st.autoplay) return;
      const cur = Math.max(0, ids.indexOf(st.selected));
      const next = ids[(cur + 1) % ids.length];
      if (next !== st.selected) {
        selectSource(next);
        const stage = body.querySelector(".vis-stage-wrap");
        const list = body.querySelector(".vis-src-list");
        if (stage) renderStage(stage);
        if (list) renderSources(list);
      }
      schedulePlaylist();
    }, 4500);
  }

  function schedule() {
    clearTimeout(st.timer);
    if (st.tab !== "wall") return;
    const live = st.sources.some((s) => s.live);
    const imageQueueLive = st.queue.some((id) => st.sources.find((s) => s.id === id)?.live);
    st.timer = setTimeout(async () => {
      if (st.selected) {
        try {
          st.preview = await api(`/api/vision/sources/${st.selected}/preview?advance=1`);
          st.sources = st.sources.map((s) => (s.id === st.preview.id ? { ...s, ...st.preview } : s));
        } catch {
          /* 预览失败时下一轮再试 */
        }
      }
      try {
        st.alerts = (await api("/api/vision/alerts")).alerts;
        loadEventCount();
      } catch {
        /* 告警刷新失败不影响画面 */
      }
      if (st.tab !== "wall") return;
      const list = body.querySelector(".vis-src-list");
      const stage = body.querySelector(".vis-stage-wrap");
      const side = body.querySelector(".vis-alerts");
      const alertHead = body.querySelector(".vis-alert-head");
      if (!list || !stage || !side) {
        render();
        return;
      }
      renderSources(list);
      const media = stage.querySelector(".vis-media");
      const overlay = stage.querySelector(".vis-overlay");
      const hud = stage.querySelector(".vis-hud");
      const src = st.sources.find((s) => s.id === st.selected);
      if (media && overlay && hud && src) {
        // 切图时换底图
        if (isImageSrc(src)) {
          const img = media.querySelector("img");
          if (!img || img.getAttribute("src") !== src.media_url) fill(media, h("img", { src: src.media_url, alt: src.name }));
        }
        paintMedia(media, overlay, hud, src);
      } else renderStage(stage);
      if (alertHead) renderAlertHead(alertHead);
      renderAlerts(side);
      schedule();
    }, live || imageQueueLive ? 700 : 4000);
  }

  /* ---------- 规则 ---------- */

  function renderRules() {
    const list = h("div", { class: "vis-rule-list" });
    const editor = h("div", { class: "vis-rule-editor" });
    fill(
      body,
      h("div", { class: "vis-compose card" }, composeBox()),
      h("div", { class: "vis-rules" }, h("div", { class: "vis-col vis-col-src" }, h("div", { class: "vis-col-head" }, "已配置规则"), list), editor),
    );
    renderRuleList(list);
    fill(editor, ruleForm(null));
  }

  function composeBox() {
    const area = h("textarea", { class: "textarea", rows: 3, placeholder: "例如：给杆塔3拉流，检测未系安全带和挂钩，置信度 0.6 以上告警，推给安全巡检助手" });
    const hint = h("div", { class: "hint" });
    const fileInput = h("input", { type: "file", accept: ".txt,.md,text/plain,text/markdown", hidden: true });
    fileInput.addEventListener("change", async () => {
      const file = fileInput.files?.[0];
      if (!file) return;
      try {
        area.value = await file.text();
        toast(`已导入 ${file.name}`, "ok");
      } catch (err) {
        toast(err.message || "读取文件失败", "error");
      }
      fileInput.value = "";
    });
    const run = async (apply) => {
      try {
        const res = await api("/api/vision/rules/compose", { method: "POST", body: { instruction: area.value, apply, use_model: true } });
        const d = res.draft;
        fill(
          hint,
          `${d.source_name || "全部视频"} · ${d.algorithms.map((id) => algoLabel(id, st.catalog.algorithms)).join("、")} · 阈值 ${d.confidence} · ${DET_LABEL[d.detector] || d.detector}`,
          d.auto_pull ? " · 将自动拉流" : "",
          d.agent_name ? ` · 推给${d.agent_name}` : "",
          d.model_hint ? h("div", { class: "muted small", style: { marginTop: "4px" } }, d.model_hint) : null,
        );
        if (res.applied) {
          toast("规则已启用", "ok");
          if (d.auto_pull && d.source_id) {
            st.tab = "wall";
            st.selected = d.source_id;
            replacePath(`/vision/${d.source_id}`);
          }
          await reload();
        }
        return res.draft;
      } catch (err) {
        toast(err.message, "error");
        return null;
      }
    };
    return h(
      "div",
      { class: "form" },
      h(
        "div",
        { class: "form-section-head" },
        h("h3", null, "配置检测规则"),
        h("p", null, "可用自然语言或导入文本。有对话模型时会辅助填表；没有也能用关键词解析。大模型不是必选项。"),
      ),
      area,
      hint,
      h(
        "div",
        { class: "form-actions", style: { justifyContent: "flex-start", flexWrap: "wrap", gap: "8px" } },
        h("button", { class: "btn", type: "button", onclick: () => fileInput.click() }, fileInput, icon("upload", 15), "导入文本"),
        h("button", { class: "btn", type: "button", onclick: () => run(false) }, icon("sparkles", 15), "预览配置"),
        h("button", { class: "btn btn-primary", type: "button", onclick: () => run(true) }, icon("zap", 15), "保存并启用"),
        !st.vlm.ready
          ? h("button", { class: "btn btn-sm", type: "button", onclick: () => goConfigureVlm("/vision/rules") }, "可选：配置大模型")
          : null,
      ),
    );
  }

  function renderRuleList(list) {
    if (!st.rules.length) {
      fill(list, h("div", { class: "vis-empty" }, "还没有规则"));
      return;
    }
    fill(
      list,
      ...st.rules.map((rule) =>
        h(
          "button",
          {
            class: "vis-src",
            type: "button",
            onclick: () => {
              const editor = body.querySelector(".vis-rule-editor");
              if (editor) fill(editor, ruleForm(rule));
            },
          },
          h("div", { class: "vis-src-top" }, h("b", null, rule.name), rule.enabled ? badge("启用", "ok") : badge("停用", "plain")),
          h("div", { class: "vis-src-sub" }, rule.source_name || "全部视频", " · ", DET_LABEL[rule.detector] || rule.detector, " · ", (rule.algorithms || []).map((id) => algoLabel(id, st.catalog.algorithms)).join("、")),
        ),
      ),
    );
  }

  function ruleForm(rule) {
    const algorithms = st.catalog.algorithms.length ? st.catalog.algorithms : ALG_FALLBACK.map(([id, name]) => ({ id, name }));
    const selected = new Set(rule?.algorithms || []);
    const name = h("input", { class: "input", value: rule?.name || "", maxlength: 80, placeholder: "规则名称" });
    const source = h(
      "select",
      { class: "select" },
      h("option", { value: "" }, "全部可见媒体"),
      ...st.sources.map((s) => h("option", { value: s.id, selected: rule?.source_id === s.id }, s.name)),
    );
    const agent = h(
      "select",
      { class: "select" },
      h("option", { value: "" }, "不推送到智能体"),
      ...store.agents.filter((a) => a.allow_events).map((a) => h("option", { value: a.id, selected: rule?.agent_id === a.id }, `${a.emoji} ${a.name}`)),
    );
    const level = h(
      "select",
      { class: "select" },
      ...["high", "medium", "low"].map((v) => h("option", { value: v, selected: (rule?.level || "medium") === v }, { high: "高级", medium: "中级", low: "低级" }[v])),
    );
    let detector = rule?.detector || "small";
    let smallModel = rule?.small_model || "demo";
    let vlmModel = rule?.vlm_model || "";
    let enabled = rule?.enabled !== false;
    let autoPush = rule?.auto_push !== false;
    let autoPull = Boolean(rule?.auto_pull);
    const conf = h("input", { class: "range", type: "range", min: "0.1", max: "0.99", step: "0.01", value: String(rule?.confidence ?? 0.55) });
    const confVal = h("span", { class: "range-value" }, conf.value);
    conf.addEventListener("input", () => (confVal.textContent = conf.value));
    const cool = h("input", { class: "input input-num", type: "number", min: "3", max: "300", value: String(rule?.cooldown_sec || 20) });
    const checks = h(
      "div",
      { class: "check-list" },
      ...algorithms.map((item) => {
        const on = selected.has(item.id);
        const box = h("input", { type: "checkbox", checked: on });
        box.addEventListener("change", () => {
          if (box.checked) selected.add(item.id);
          else selected.delete(item.id);
        });
        return h("label", { class: "check-item" }, box, h("div", { class: "check-main" }, h("b", null, item.name), h("span", { class: "muted small" }, item.id)));
      }),
    );
    const modelPick = h("div", { class: "vis-model-pick" });
    const vlmNote = h("div", { class: "hint" });
    const paintModelPick = () => {
      const smalls = st.catalog.small_models?.length ? st.catalog.small_models : [{ id: "demo", name: "场景演示" }, { id: "yolo", name: "YOLO" }];
      const visions = st.catalog.vision_models || [];
      const showSmall = detector === "small" || detector === "small_then_vlm";
      const showVlm = needsVlm(detector);
      fill(
        modelPick,
        showSmall
          ? field(
              "小模型",
              h(
                "select",
                { class: "select", onchange: (e) => (smallModel = e.target.value) },
                ...smalls.map((m) => h("option", { value: m.id, selected: m.id === smallModel }, m.ready === false ? `${m.name}（未就绪）` : m.name)),
              ),
              "YOLO 用 data/models 下的 .pt 权重，类名对齐算法 id。",
            )
          : null,
        showVlm
          ? field(
              "大模型",
              h(
                "select",
                { class: "select", onchange: (e) => (vlmModel = e.target.value) },
                h("option", { value: "", selected: !vlmModel }, "默认视觉模型"),
                ...visions.map((m) => h("option", { value: m.ref, selected: m.ref === vlmModel }, `${m.label}${m.vision ? "" : "（未标视觉）"}`)),
              ),
            )
          : null,
      );
      if (!needsVlm(detector)) {
        const chosen = smalls.find((m) => m.id === smallModel);
        fill(vlmNote, chosen?.hint || "小模型按所选类型检测。");
        return;
      }
      if (st.vlm.ready) {
        fill(vlmNote, "大模型已配置。可在上方指定具体看图模型。");
        return;
      }
      fill(vlmNote, "还没有可用的大模型。", " ", h("button", { class: "btn btn-sm", type: "button", onclick: () => goConfigureVlm("/vision/rules") }, "去配置"));
    };
    paintModelPick();
    const save = async () => {
      const body = {
        name: name.value.trim() || "未命名规则",
        source_id: source.value ? Number(source.value) : null,
        agent_id: agent.value ? Number(agent.value) : null,
        algorithms: [...selected],
        confidence: Number(conf.value),
        cooldown_sec: Number(cool.value) || 20,
        level: level.value,
        detector,
        small_model: smallModel || "demo",
        vlm_model: vlmModel || "",
        enabled,
        auto_push: autoPush,
        auto_pull: autoPull,
        instruction: rule?.instruction || "",
      };
      try {
        if (rule) await api(`/api/vision/rules/${rule.id}`, { method: "PATCH", body });
        else await api("/api/vision/rules", { method: "POST", body });
        toast("规则已保存", "ok");
        await reload();
      } catch (err) {
        toast(err.message, "error");
      }
    };
    return h(
      "div",
      { class: "form-section" },
      h("div", { class: "form-section-head" }, h("h3", null, rule ? "编辑规则" : "手动新建"), h("p", null, "绑定媒体、算法、检测模式。小模型默认可用；大模型可选。")),
      field("名称", name),
      field("媒体点位", source),
      field("检测算法", checks),
      field("告警级别", level),
      field(
        "检测模式",
        segmented(
          [
            ["small", "仅小模型"],
            ["vlm", "仅大模型"],
            ["small_then_vlm", "小模型+后处理"],
          ],
          detector,
          (v) => {
            detector = v;
            paintModelPick();
          },
        ),
      ),
      modelPick,
      vlmNote,
      field("置信度阈值", h("div", { class: "vis-range" }, conf, confVal)),
      field("告警冷却（秒）", cool),
      field("处置智能体", agent, "命中后把告警推到事件中心，由该智能体按规程写处置建议。"),
      switchInput(enabled, (v) => (enabled = v), "启用这条规则"),
      switchInput(autoPush, (v) => (autoPush = v), "命中后自动推送到事件中心"),
      switchInput(autoPull, (v) => (autoPull = v), "保存后自动按点位拉流"),
      h(
        "div",
        { class: "form-actions" },
        rule
          ? h(
              "button",
              {
                class: "btn btn-danger",
                type: "button",
                onclick: async () => {
                  const ok = await confirmDialog({ title: "删除规则", message: `删除「${rule.name}」？`, confirmText: "删除", danger: true });
                  if (!ok) return;
                  await api(`/api/vision/rules/${rule.id}`, { method: "DELETE" });
                  toast("已删除", "ok");
                  reload();
                },
              },
              "删除",
            )
          : null,
        h("div", { class: "grow" }),
        h("button", { class: "btn btn-primary", type: "button", onclick: save }, "保存"),
      ),
    );
  }

  renderTabs();
  fill(body, h("div", { class: "loading-row" }, spinner(16), "加载视觉检测…"));
  reload();
  return { el, destroy: () => clearTimeout(st.timer) };
}
