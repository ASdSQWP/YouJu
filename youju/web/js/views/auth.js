import { api } from "../api.js";
import { fill, field, h, icon, logo, spinner } from "../ui.js";

function hero() {
  const point = (ic, title, desc) => h("div", { class: "auth-point" }, h("span", { class: "auth-point-icon" }, icon(ic, 18)), h("div", null, h("b", null, title), h("span", null, desc)));
  return h(
    "div",
    { class: "auth-hero" },
    h("div", { class: "auth-brand light" }, logo(30), h("span", null, "有据"), h("span", { class: "brand-tag" }, "YouJu")),
    h(
      "div",
      null,
      h("h1", null, "每一句结论，", h("br"), "都找得到出处。"),
      h("p", null, "把制度、规程、手册变成会回答问题的智能体。回答逐句标注来源，没有依据就不作答。"),
      h(
        "div",
        { class: "auth-points" },
        point("shield", "逐句引用核验", "每句结论都和引用原文比对，依据不足的句子会单独标出来"),
        point("plug", "20 多家模型服务", "DeepSeek、通义千问、智谱、Kimi、豆包、GPT、Claude、Gemini 都能接，也支持本地 Ollama"),
        point("inbox", "事件驱动处置", "告警、工单推送进来，自动生成带依据的处置建议，人工确认后执行"),
        point("link", "开放接入", "提供 OpenAI 兼容接口和 MCP 服务，能直接接入现有系统和 AI 客户端"),
      ),
    ),
    h(
      "div",
      { class: "auth-demo" },
      h("div", { class: "auth-demo-q" }, "安全带没扣好可以继续干活吗？"),
      h(
        "div",
        { class: "auth-demo-a" },
        "不可以。安全带未扣好时必须立即停止作业，人员先回到稳固位置再重新穿戴",
        h("span", { class: "cite" }, "1"),
        "。",
        h("div", { class: "auth-demo-foot" }, h("span", { class: "check-badge check-pass" }, icon("shield", 13), "引用核验通过 1/1"), h("span", { class: "demo-src" }, "[1] 高空作业安全规程 · 安全带与挂钩")),
      ),
    ),
  );
}

export function renderAuth(root, initialized, onDone) {
  const error = h("div", { class: "form-error", hidden: true });
  const submit = h("button", { class: "btn btn-primary btn-lg btn-block", type: "submit" }, initialized ? "登录" : "创建管理员并进入");
  const username = h("input", { class: "input input-lg", autocomplete: "username", placeholder: initialized ? "用户名" : "例如 admin", required: true });
  const password = h("input", { class: "input input-lg", type: "password", autocomplete: initialized ? "current-password" : "new-password", placeholder: initialized ? "密码" : "至少 6 位", required: true });
  const displayName = h("input", { class: "input input-lg", placeholder: "例如 张经理" });
  const confirm = h("input", { class: "input input-lg", type: "password", autocomplete: "new-password", placeholder: "再输入一次" });
  const seed = h("input", { type: "checkbox", checked: true });

  const fields = initialized
    ? [field("用户名", username), field("密码", password)]
    : [
        field("管理员账号", username, "2–32 位字母、数字或 _ . @ -"),
        field("显示名称", displayName),
        field("密码", password),
        field("确认密码", confirm),
        h("label", { class: "check-item seed" }, seed, h("span", { class: "check-main" }, h("b", null, "导入示例数据"), h("span", { class: "muted small" }, "4 个行业知识库（安全生产、IT 运维、员工制度、客户服务）和 5 个智能体，内容均为演示虚构"))),
      ];

  const form = h(
    "form",
    {
      class: "auth-form",
      onsubmit: async (e) => {
        e.preventDefault();
        error.hidden = true;
        if (!initialized && password.value !== confirm.value) {
          error.textContent = "两次输入的密码不一致";
          error.hidden = false;
          return;
        }
        submit.disabled = true;
        fill(submit, spinner(15), initialized ? " 登录中…" : " 正在初始化，导入示例资料…");
        try {
          if (initialized) await api("/api/auth/login", { method: "POST", body: { username: username.value.trim(), password: password.value }, quiet: true });
          else await api("/api/auth/setup", { method: "POST", body: { username: username.value.trim(), password: password.value, display_name: displayName.value.trim(), seed_demo: seed.checked }, quiet: true });
          onDone();
        } catch (err) {
          error.textContent = err.message;
          error.hidden = false;
          submit.disabled = false;
          submit.textContent = initialized ? "登录" : "创建管理员并进入";
        }
      },
    },
    ...fields,
    error,
    submit,
  );

  fill(root, 
    h(
      "div",
      { class: "auth" },
      hero(),
      h(
        "div",
        { class: "auth-panel" },
        h(
          "div",
          { class: "auth-card" },
          h("div", { class: "auth-brand only-narrow" }, logo(30), h("span", null, "有据")),
          h("h2", null, initialized ? "欢迎回来" : "初始化「有据」"),
          h("p", { class: "sub" }, initialized ? "登录后继续使用团队的智能体和知识库。" : "第一个账号是管理员，之后可以在「设置 → 成员管理」邀请同事。"),
          form,
          h("p", { class: "auth-foot muted small" }, "数据只保存在本机 data 目录，未接入模型时也能离线演示。"),
        ),
      ),
    ),
  );
  setTimeout(() => username.focus(), 50);
}
