let unauthorizedHandler = null;

export function onUnauthorized(fn) {
  unauthorizedHandler = fn;
}

export class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

function detailOf(data, status) {
  if (data && typeof data === "object" && typeof data.detail === "string") return data.detail;
  return `请求失败（${status}）`;
}

export async function api(path, { method = "GET", body, form, signal, quiet = false } = {}) {
  const init = { method, headers: {}, signal, credentials: "same-origin" };
  if (form) init.body = form;
  else if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(path, init);
  } catch (err) {
    if (err.name === "AbortError") throw err;
    throw new ApiError(0, "连接不上服务，请确认后端已经启动");
  }
  const text = await res.text();
  let data = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
  }
  if (!res.ok) {
    if (res.status === 401 && !quiet && unauthorizedHandler) unauthorizedHandler();
    throw new ApiError(res.status, detailOf(data, res.status));
  }
  return data;
}

function emitSSE(block, onEvent) {
  for (const raw of block.split("\n")) {
    if (!raw.startsWith("data:")) continue;
    const payload = raw.slice(5).trim();
    if (!payload || payload === "[DONE]") continue;
    try {
      onEvent(JSON.parse(payload));
    } catch {
      /* 半包或注释行 */
    }
  }
}

export async function streamSSE(path, body, onEvent, signal) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify(body),
    signal,
    credentials: "same-origin",
    cache: "no-store",
  });
  if (!res.ok) {
    let data = null;
    try {
      data = await res.json();
    } catch {
      /* 不是 JSON */
    }
    if (res.status === 401 && unauthorizedHandler) unauthorizedHandler();
    throw new ApiError(res.status, detailOf(data, res.status));
  }
  if (!res.body) throw new ApiError(0, "浏览器没有返回流式响应");
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  const pump = (flush) => {
    buffer = buffer.replace(/\r\n/g, "\n");
    let cut;
    while ((cut = buffer.indexOf("\n\n")) >= 0) {
      emitSSE(buffer.slice(0, cut), onEvent);
      buffer = buffer.slice(cut + 2);
    }
    if (flush && buffer.trim()) {
      emitSSE(buffer, onEvent);
      buffer = "";
    }
  };
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (value) buffer += decoder.decode(value, { stream: !done });
      if (done) {
        buffer += decoder.decode();
        pump(true);
        break;
      }
      pump(false);
    }
  } finally {
    try {
      reader.releaseLock();
    } catch {
      /* 已经释放 */
    }
  }
}
