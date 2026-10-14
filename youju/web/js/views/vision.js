import { api } from "../api.js";
import { fill, badge, confirmDialog, emptyState, field, h, icon, openSidebar, segmented, spinner, switchInput, timeAgo, toast } from "../ui.js?v=20261015b";
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

function algoLabel(id, catalog) {
  return (catalog.find((a) => a.id === id) || {}).name || ALG_FALLBACK.find((a) => a[0] === id)?.[1] || id;
}

export function visionView({ params, path }) {
  const st = {
    tab: path.startsWith("/vision/rules") ? "rules" : "wall",
    selected: params.id && params.id !== "rules" ? Number(params.id) : null,
    sources: [],
    rules: [],
    alerts: [],
    catalog: { algorithms: [], detectors: [] },
    preview: null,
    timer: null,
    pullName: "",
  };
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
        h("div", null, h("h1", null, "视觉检测"), h("p", null, "按视频名称拉流，用自然语言或表单配置检测规则。告警会推给安全巡检助手，由它按规程给出处置建议。")),
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
      h("button", { class: `ev-tab${st.tab === "wall" ? " active" : ""}`, type: "button", onclick: () => switchTab("wall") }, icon("video", 14), "监视墙"),
      h("button", { class: `ev-tab${st.tab === "rules" ? " active" : ""}`, type: "button", onclick: () => switchTab("rules") }, icon("crosshair", 14), "检测规则"),
    );
  }

  function switchTab(tab) {
    st.tab = tab;
    replacePath(tab === "rules" ? "/vision/rules" : st.selected ? `/vision/${st.selected}` : "/vision");
    renderTabs();
    render();
  }

  async function reload() {
    try {
      const [catalog, sources, rules, alerts] = await Promise.all([
        api("/api/vision/catalog"),
        api("/api/vision/sources"),
        api("/api/vision/rules"),
        api("/api/vision/alerts"),
      ]);
      st.catalog = catalog;
      st.sources = sources.sources;
      st.rules = rules.rules;
      st.alerts = alerts.alerts;
      if (!st.selected && st.sources.length) st.selected = st.sources[0].id;
      renderTabs();
      render();
    } catch (err) {
      fill(body, h("div", { class: "notice notice-danger" }, err.message));
    }
  }

  function render() {
    if (st.tab === "rules") renderRules();
    else renderWall();
  }

  function renderWall() {
    const list = h("div", { class: "vis-src-list" });
    const stage = h("div", { class: "vis-stage-wrap" });
    const side = h("div", { class: "vis-alerts" });
    fill(
      body,
      h(
        "div",
        { class: "vis-layout" },
        h(
          "div",
          { class: "vis-col vis-col-src" },
          h("div", { class: "vis-col-head" }, h("span", null, "视频点位"), uploadBtn()),
          list,
        ),
        stage,
        h("div", { class: "vis-col vis-col-alert" }, h("div", { class: "vis-col-head" }, "实时告警"), side),
      ),
    );
    renderSources(list);
    renderStage(stage);
    renderAlerts(side);
    schedule();
  }

  function uploadBtn() {
    const input = h("input", { type: "file", accept: "image/*,video/mp4,video/webm", multiple: true, hidden: true });
    input.addEventListener("change", async () => {
      if (!input.files?.length) return;
      const form = new FormData();
      for (const file of input.files) form.append("files", file);
      try {
        const res = await api("/api/vision/sources/upload", { method: "POST", form });
        if (res.created?.length) {
          toast(`已添加 ${res.created.length} 路视频`, "ok");
          st.selected = res.created[0].id;
        }
        if (res.skipped?.length) toast(res.skipped[0].reason, "error");
        await reload();
      } catch (err) {
        toast(err.message, "error");
      }
      input.value = "";
    });
    return h("button", { class: "btn btn-sm", type: "button", onclick: () => input.click() }, input, icon("upload", 14), "上传");
  }

  function renderSources(list) {
    if (!st.sources.length) {
      fill(list, h("div", { class: "vis-empty" }, "还没有点位"));
      return;
    }
    fill(
      list,
      ...st.sources.map((src) =>
        h(
          "button",
          {
            class: `vis-src${src.id === st.selected ? " active" : ""}`,
            type: "button",
            onclick: () => {
              st.selected = src.id;
              replacePath(`/vision/${src.id}`);
              render();
            },
          },
          h("div", { class: "vis-src-top" }, h("span", { class: `vis-dot${src.live ? " on" : ""}` }), h("b", null, src.name), src.live ? badge("LIVE", "danger") : badge(src.kind === "file" ? "上传" : "演示", "plain")),
          h("div", { class: "vis-src-sub" }, src.code || src.filename || src.uri || "未命名通道"),
        ),
      ),
    );
  }

  function renderStage(stage) {
    const src = st.sources.find((s) => s.id === st.selected);
    if (!src) {
      fill(stage, emptyState("camera", "选择左侧点位开始监视", "也可以在右上角输入视频名称拉流，例如「杆塔3」。"));
      return;
    }
    const media = h("div", { class: "vis-media" });
    const overlay = h("div", { class: "vis-overlay" });
    const hud = h("div", { class: "vis-hud" });
    fill(
      stage,
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
            : h("button", { class: "btn btn-sm btn-primary", type: "button", onclick: () => startSrc(src) }, icon("play", 14), "拉流"),
          src.kind === "file"
            ? h(
                "button",
                {
                  class: "btn btn-sm btn-danger",
                  type: "button",
                  onclick: async () => {
                    const ok = await confirmDialog({ title: "删除点位", message: `删除「${src.name}」？`, confirmText: "删除", danger: true });
                    if (!ok) return;
                    await api(`/api/vision/sources/${src.id}`, { method: "DELETE" });
                    st.selected = null;
                    reload();
                  },
                },
                icon("trash", 14),
                "删除",
              )
            : null,
        ),
      ),
      h("p", { class: "hint vis-caption" }, "演示点位按场景回放检测结果，可换成真实拉流和小模型 / 视觉大模型。"),
    );
    paintMedia(media, overlay, hud, src);
  }

  function paintMedia(media, overlay, hud, src) {
    const preview = st.preview?.id === src.id ? st.preview : src;
    const boxes = preview.boxes || [];
    const kind = src.preview_kind;
    if (kind === "video" && src.media_url) {
      if (!media.querySelector("video")) fill(media, h("video", { src: src.media_url, autoplay: true, muted: true, loop: true, playsinline: true, controls: true }));
    } else if (kind === "image" && src.media_url) {
      if (!media.querySelector("img")) fill(media, h("img", { src: src.media_url, alt: src.name }));
    } else {
      const url = `${src.frame_url}?t=${Date.now()}`;
      const img = media.querySelector("img");
      if (img) img.src = url;
      else fill(media, h("img", { src: url, alt: src.name }));
    }
    paintBoxes(overlay, hud, src, boxes);
  }

  function paintBoxes(overlay, hud, src, boxes) {
    fill(
      overlay,
      ...(boxes || []).map((box) => {
        const [x, y, bw, bh] = box.box || [0, 0, 0, 0];
        return h(
          "div",
          { class: `vis-box${box.level === "high" ? " high" : ""}`, style: { left: `${x * 100}%`, top: `${y * 100}%`, width: `${bw * 100}%`, height: `${bh * 100}%` } },
          h("span", null, `${box.name} ${Math.round(box.confidence * 100)}%`),
        );
      }),
    );
    fill(
      hud,
      h("span", { class: `vis-live${src.live ? " on" : ""}` }, src.live ? "LIVE" : "IDLE"),
      h("span", null, src.name),
      h("span", { class: "muted" }, src.code || ""),
      boxes?.length ? h("span", { class: "vis-hit" }, `${boxes.length} 个目标`) : h("span", { class: "muted" }, "画面中无命中规则的目标"),
    );
  }

  function renderAlerts(side) {
    const rows = st.selected ? st.alerts.filter((a) => a.source_id === st.selected) : st.alerts;
    if (!rows.length) {
      fill(side, h("div", { class: "vis-empty" }, "拉流后命中规则就会在这里出告警，并推到事件中心。"));
      return;
    }
    fill(
      side,
      ...rows.map((a) =>
        h(
          "button",
          {
            class: "vis-alert",
            type: "button",
            onclick: () => {
              if (a.event_id) navigate(`/events/${a.event_id}`);
            },
          },
          h("div", { class: "ev-row" }, levelBadge(a.level), badge(a.detector === "vlm" ? "大模型" : "小模型", "plain"), h("span", { class: "muted small grow-right" }, timeAgo(a.created_at))),
          h("div", { class: "ev-title" }, a.title),
          h("div", { class: "ev-sub" }, a.findings.join("、"), a.event_id ? h("span", { class: "ok-text" }, " 已推送") : null),
        ),
      ),
    );
  }

  async function startSrc(src) {
    try {
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

  function schedule() {
    clearTimeout(st.timer);
    if (st.tab !== "wall") return;
    const live = st.sources.some((s) => s.live);
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
      if (!list || !stage || !side) {
        render();
        return;
      }
      renderSources(list);
      const media = stage.querySelector(".vis-media");
      const overlay = stage.querySelector(".vis-overlay");
      const hud = stage.querySelector(".vis-hud");
      const src = st.sources.find((s) => s.id === st.selected);
      if (media && overlay && hud && src) paintMedia(media, overlay, hud, src);
      else renderStage(stage);
      renderAlerts(side);
      schedule();
    }, live ? 900 : 4000);
  }

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
    const run = async (apply) => {
      try {
        const res = await api("/api/vision/rules/compose", { method: "POST", body: { instruction: area.value, apply } });
        const d = res.draft;
        fill(
          hint,
          `${d.source_name || "全部视频"} · ${d.algorithms.map((id) => algoLabel(id, st.catalog.algorithms)).join("、")} · 阈值 ${d.confidence} · ${d.detector === "vlm" ? "大模型" : "小模型"}`,
          d.auto_pull ? " · 将自动拉流" : "",
          d.agent_name ? ` · 推给${d.agent_name}` : "",
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
      h("div", { class: "form-section-head" }, h("h3", null, "让智能体配置规则"), h("p", null, "用一句话描述点位、检测项和要不要拉流。也可以在下面手动改。")),
      area,
      hint,
      h(
        "div",
        { class: "form-actions", style: { justifyContent: "flex-start" } },
        h("button", { class: "btn", type: "button", onclick: () => run(false) }, icon("sparkles", 15), "预览配置"),
        h("button", { class: "btn btn-primary", type: "button", onclick: () => run(true) }, icon("zap", 15), "保存并启用"),
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
          h("div", { class: "vis-src-sub" }, rule.source_name || "全部视频", " · ", (rule.algorithms || []).map((id) => algoLabel(id, st.catalog.algorithms)).join("、")),
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
      h("option", { value: "" }, "全部视频"),
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
      h("div", { class: "form-section-head" }, h("h3", null, rule ? "编辑规则" : "手动新建"), h("p", null, "算法、阈值、冷却时间和推送智能体都可以在这里改。")),
      field("名称", name),
      field("视频点位", source),
      field("检测算法", checks),
      field("告警级别", level),
      field("检测器", segmented([["small", "小模型"], ["vlm", "视觉大模型"]], detector, (v) => (detector = v))),
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
