"""连接外部 MCP（Streamable HTTP / JSON-RPC）。"""

from __future__ import annotations

import httpx

from youju.providers import http


def _post(url: str, method: str, msg_id: int, params: dict | None = None) -> dict:
    body = {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}}
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    with http.sync_client(timeout=httpx.Timeout(20.0)) as client:
        resp = client.post(url, json=body, headers=headers)
    if resp.status_code >= 400:
        raise RuntimeError(f"MCP {resp.status_code}: {resp.text[:180]}")
    data = resp.json()
    if isinstance(data, dict) and data.get("error"):
        err = data["error"]
        raise RuntimeError(str(err.get("message") if isinstance(err, dict) else err))
    return data if isinstance(data, dict) else {}


def list_tools(url: str) -> list[dict]:
    _post(url, "initialize", 1, {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "youju", "version": "1"}})
    data = _post(url, "tools/list", 2, {})
    result = data.get("result") or {}
    tools = result.get("tools") if isinstance(result, dict) else None
    if not isinstance(tools, list):
        return []
    out = []
    for tool in tools:
        if not isinstance(tool, dict) or not tool.get("name"):
            continue
        out.append(
            {
                "name": str(tool.get("name"))[:80],
                "description": str(tool.get("description") or "")[:240],
            }
        )
    return out[:40]


def call_tool(url: str, name: str, arguments: dict | None = None) -> str:
    data = _post(url, "tools/call", 3, {"name": name, "arguments": arguments or {}})
    result = data.get("result") or {}
    if isinstance(result, dict) and result.get("isError"):
        content = result.get("content") or []
        text = content[0].get("text") if content and isinstance(content[0], dict) else "工具返回错误"
        raise RuntimeError(str(text)[:400])
    content = result.get("content") if isinstance(result, dict) else None
    if isinstance(content, list):
        parts = [str(part.get("text") or "") for part in content if isinstance(part, dict)]
        return "\n".join(p for p in parts if p)[:4000] or "工具已执行，没有文本结果"
    return str(result)[:4000]
