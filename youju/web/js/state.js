import { api } from "./api.js";

export const store = {
  user: null,
  version: "",
  agents: [],
  kbs: [],
  conversations: [],
  models: { chat: [], embedding: [], rerank: [], defaults: {} },
  pendingEvents: 0,
};

const listeners = {};

export function on(name, fn) {
  (listeners[name] ||= new Set()).add(fn);
  return () => listeners[name].delete(fn);
}

export function emit(name, data) {
  listeners[name]?.forEach((fn) => fn(data));
}

export const isAdmin = () => store.user?.role === "admin";

export async function loadAgents() {
  store.agents = (await api("/api/agents")).agents;
  emit("agents");
}

export async function loadConversations() {
  store.conversations = (await api("/api/conversations")).conversations;
  emit("conversations");
}

export async function loadModels() {
  store.models = await api("/api/models");
  emit("models");
}

export async function loadKbs() {
  store.kbs = (await api("/api/kbs")).kbs;
  emit("kbs");
}

export async function loadEventCount() {
  try {
    const data = await api("/api/events?status=pending");
    store.pendingEvents = data.counts.pending || 0;
    emit("events");
  } catch {
    /* 事件计数失败不影响其它页面 */
  }
}

export async function loadAll() {
  await Promise.all([loadAgents(), loadConversations(), loadModels(), loadKbs(), loadEventCount()]);
}
