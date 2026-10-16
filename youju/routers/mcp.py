"""MCP 服务端（Streamable HTTP，JSON 响应）。

Cursor、Claude Desktop 等 MCP 客户端配好地址和 API Key 后，可以直接检索知识库、调用智能体。
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from youju import __version__
from youju.agent.runner import AgentSnapshot, RunInput, collect
from youju.db import SessionLocal
from youju.deps import current_user, visible
from youju.db import get_db
from youju.mcp_remote import list_tools
from youju.models import Agent, KnowledgeBase, McpClient, User
from youju.rag.retrieve import retrieve
from youju.routers.openai_api import references

router = APIRouter(tags=["mcp"])


class McpClientBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    url: str = Field(min_length=8, max_length=500)


def _client_public(row: McpClient) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "url": row.url,
        "enabled": row.enabled,
        "tools": list(row.tools or []),
        "last_error": row.last_error or "",
    }


@router.get("/api/mcp/clients")
def list_mcp_clients(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(McpClient).where(McpClient.owner_id == user.id).order_by(McpClient.id)).all()
    return {"clients": [_client_public(row) for row in rows]}


@router.post("/api/mcp/clients")
def add_mcp_client(body: McpClientBody, user: User = Depends(current_user), db: Session = Depends(get_db)):
    url = body.url.strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="MCP 地址需要以 http:// 或 https:// 开头")
    row = McpClient(owner_id=user.id, name=body.name.strip()[:80], url=url, enabled=True, tools=[], last_error="")
    try:
        row.tools = list_tools(url)
    except Exception as exc:  # noqa: BLE001
        row.last_error = str(exc)[:300]
        row.enabled = False
    db.add(row)
    db.commit()
    return _client_public(row)


@router.post("/api/mcp/clients/{client_id}/refresh")
def refresh_mcp_client(client_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    row = db.get(McpClient, client_id)
    if row is None or row.owner_id != user.id:
        raise HTTPException(status_code=404, detail="MCP 服务不存在")
    try:
        row.tools = list_tools(row.url)
        row.last_error = ""
        row.enabled = True
    except Exception as exc:  # noqa: BLE001
        row.last_error = str(exc)[:300]
    db.commit()
    return _client_public(row)


@router.patch("/api/mcp/clients/{client_id}")
def patch_mcp_client(client_id: int, user: User = Depends(current_user), db: Session = Depends(get_db), enabled: bool = True):
    row = db.get(McpClient, client_id)
    if row is None or row.owner_id != user.id:
        raise HTTPException(status_code=404, detail="MCP 服务不存在")
    row.enabled = enabled
    db.commit()
    return _client_public(row)


@router.delete("/api/mcp/clients/{client_id}")
def delete_mcp_client(client_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    row = db.get(McpClient, client_id)
    if row is None or row.owner_id != user.id:
        raise HTTPException(status_code=404, detail="MCP 服务不存在")
    db.delete(row)
    db.commit()
    return {"ok": True}

VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

TOOLS = [
    {
        "name": "list_knowledge_bases",
        "description": "列出当前账号可以访问的知识库。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "search_knowledge",
        "description": "在知识库中检索原文片段，返回带文件名和章节的片段。回答需要依据时先调用它。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索词或问题"},
                "knowledge_base": {"type": "string", "description": "知识库名称或 ID，不填则检索全部可访问的知识库"},
                "top_k": {"type": "integer", "minimum": 1, "maximum": 10, "description": "返回条数，默认 5"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "ask_agent",
        "description": "向「有据」中的智能体提问，返回带引用编号和参考资料的回答。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent": {"type": "string", "description": "智能体名称或 ID"},
                "question": {"type": "string", "description": "问题"},
            },
            "required": ["agent", "question"],
        },
    },
]


class ToolError(Exception):
    pass


def _result(msg_id, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


@router.get("/mcp")
def mcp_get():
    return Response(status_code=405, headers={"Allow": "POST"})


@router.post("/mcp")
async def mcp_post(request: Request, user: User = Depends(current_user)):
    try:
        payload = await request.json()
    except ValueError:
        return JSONResponse(_error(None, -32700, "Parse error"), status_code=400)
    if isinstance(payload, list):
        replies = [reply for reply in [await _handle(item, user) for item in payload] if reply is not None]
        return JSONResponse(replies) if replies else Response(status_code=202)
    reply = await _handle(payload, user)
    return Response(status_code=202) if reply is None else JSONResponse(reply)


async def _handle(msg, user: User) -> dict | None:
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
        return _error(msg.get("id") if isinstance(msg, dict) else None, -32600, "Invalid Request")
    method, msg_id, params = msg["method"], msg.get("id"), msg.get("params") or {}
    if msg_id is None:
        return None
    if method == "initialize":
        asked = params.get("protocolVersion")
        return _result(
            msg_id,
            {
                "protocolVersion": asked if asked in VERSIONS else VERSIONS[0],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "youju", "title": "有据知识库", "version": __version__},
                "instructions": "回答业务问题前先用 search_knowledge 查依据，引用时写明文件名。",
            },
        )
    if method == "ping":
        return _result(msg_id, {})
    if method == "tools/list":
        return _result(msg_id, {"tools": TOOLS})
    if method == "tools/call":
        try:
            text, structured = await _call(params.get("name"), params.get("arguments") or {}, user)
        except ToolError as exc:
            return _result(msg_id, {"content": [{"type": "text", "text": str(exc)}], "isError": True})
        return _result(msg_id, {"content": [{"type": "text", "text": text}], "structuredContent": structured, "isError": False})
    return _error(msg_id, -32601, f"Method not found: {method}")


def _visible_kbs(user: User) -> list[KnowledgeBase]:
    db = SessionLocal()
    try:
        return list(db.scalars(select(KnowledgeBase).where(visible(KnowledgeBase, user)).order_by(KnowledgeBase.id)).all())
    finally:
        db.close()


async def _call(name: str, args: dict, user: User) -> tuple[str, dict]:
    if name == "list_knowledge_bases":
        items = [{"id": kb.id, "name": kb.name, "description": kb.description} for kb in _visible_kbs(user)]
        text = "\n".join(f"- {i['name']}（ID {i['id']}）：{i['description']}" for i in items) or "没有可以访问的知识库。"
        return text, {"knowledge_bases": items}
    if name == "search_knowledge":
        query = str(args.get("query") or "").strip()
        if not query:
            raise ToolError("query 不能为空")
        try:
            top_k = max(1, min(int(args.get("top_k") or 5), 10))
        except (TypeError, ValueError):
            top_k = 5
        kbs = _visible_kbs(user)
        target = str(args.get("knowledge_base") or "").strip()
        if target:
            kbs = [kb for kb in kbs if target in (kb.name, str(kb.id))]
            if not kbs:
                raise ToolError(f"没有找到知识库：{target}")
        result = await asyncio.to_thread(retrieve, [kb.id for kb in kbs], query, top_k)
        if not result.hits:
            return f"没有检索到与「{query}」相关的资料。", {"hits": [], "evidence": False}
        text = "\n\n".join(
            f"[{i}] {h.filename}" + (f" · {h.section}" if h.section else "") + f"（{h.kb_name}）\n{h.text}"
            for i, h in enumerate(result.hits, start=1)
        )
        return text, {"hits": [h.public() for h in result.hits], "evidence": result.evidence, "reason": result.reason}
    if name == "ask_agent":
        question = str(args.get("question") or "").strip()
        target = str(args.get("agent") or "").strip()
        if not question or not target:
            raise ToolError("agent 和 question 都不能为空")
        db = SessionLocal()
        try:
            agents = db.scalars(select(Agent).where(visible(Agent, user)).order_by(Agent.id)).all()
            agent = next((a for a in agents if target in (a.name, str(a.id), f"agent-{a.id}")), None)
            snapshot = AgentSnapshot.of(agent) if agent else None
        finally:
            db.close()
        if snapshot is None:
            raise ToolError(f"没有找到智能体：{target}")
        final = await collect(RunInput(agent=snapshot, question=question))
        used = final.get("used") or []
        return (
            final.get("content", "") + references(final),
            {
                "answer": final.get("content", ""),
                "sources": [s for s in final.get("sources") or [] if s.get("n") in used],
                "check": final.get("check"),
                "refused": final.get("refused"),
            },
        )
    raise ToolError(f"未知工具：{name}")
