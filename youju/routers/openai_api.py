"""OpenAI 兼容接口：智能体当作模型暴露出去，支持 OpenAI 协议的客户端可以直接调用。"""

from __future__ import annotations

import json
import time
from datetime import timezone
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from youju.agent.runner import AgentSnapshot, RunInput, collect, run
from youju.answers import save_answer
from youju.db import get_db
from youju.deps import current_user, visible
from youju.models import Agent, Conversation, Message, User
from youju.routers.chat import SSE_HEADERS, short_title

router = APIRouter(tags=["openai"])


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")
    role: str
    content: Any = ""


class CompletionBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    model: str
    messages: list[ChatMessage] = Field(min_length=1)
    stream: bool = False


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict) and part.get("type") in ("text", "input_text")
        )
    return str(content or "")


def _find_agent(db: Session, user: User, name: str) -> Agent:
    key = name.strip()
    for agent in db.scalars(select(Agent).where(visible(Agent, user)).order_by(Agent.id)).all():
        if key in (f"agent-{agent.id}", str(agent.id), agent.name):
            return agent
    raise HTTPException(status_code=404, detail=f"没有找到模型 {name}，可以先调用 /v1/models 查看可用的智能体")


def references(final: dict) -> str:
    used = final.get("used") or []
    refs = [s for s in final.get("sources") or [] if s.get("n") in used]
    if not refs:
        return ""
    lines = ["", "", "参考资料："]
    lines += [f"[{s['n']}] {s['filename']}" + (f" · {s['section']}" if s.get("section") else "") for s in refs]
    return "\n".join(lines)


def _extra(final: dict) -> dict:
    used = final.get("used") or []
    return {
        "sources": [s for s in final.get("sources") or [] if s.get("n") in used],
        "check": final.get("check"),
        "refused": final.get("refused"),
        "mode": final.get("mode"),
    }


@router.get("/v1/models")
def list_models(user: User = Depends(current_user), db: Session = Depends(get_db)):
    agents = db.scalars(select(Agent).where(visible(Agent, user)).order_by(Agent.id)).all()
    return {
        "object": "list",
        "data": [
            {
                "id": f"agent-{a.id}",
                "object": "model",
                "created": int(a.created_at.replace(tzinfo=timezone.utc).timestamp()),
                "owned_by": "youju",
                "name": a.name,
                "description": a.description,
            }
            for a in agents
        ],
    }


@router.post("/v1/chat/completions")
async def completions(body: CompletionBody, user: User = Depends(current_user), db: Session = Depends(get_db)):
    agent = _find_agent(db, user, body.model)
    turns = [{"role": m.role, "content": _text(m.content)} for m in body.messages if m.role in ("user", "assistant")]
    if not turns or turns[-1]["role"] != "user" or not turns[-1]["content"].strip():
        raise HTTPException(status_code=400, detail="最后一条消息需要是非空的 user 消息")
    question = turns[-1]["content"].strip()
    conv = Conversation(user_id=user.id, agent_id=agent.id, title=short_title(question), source="api")
    db.add(conv)
    db.flush()
    db.add(Message(conv_id=conv.id, role="user", content=question))
    db.commit()
    inp = RunInput(agent=AgentSnapshot.of(agent), question=question, history=turns[:-1])
    completion_id = f"chatcmpl-{uuid4().hex[:24]}"
    created = int(time.time())
    model_name = f"agent-{agent.id}"
    if body.stream:
        return StreamingResponse(
            _stream(conv.id, inp, completion_id, created, model_name), media_type="text/event-stream", headers=SSE_HEADERS
        )
    final = await collect(inp)
    save_answer(conv.id, final=final)
    usage = final.get("usage") or {}
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model_name,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": final.get("content", "") + references(final)},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": usage.get("input", 0),
            "completion_tokens": usage.get("output", 0),
            "total_tokens": usage.get("input", 0) + usage.get("output", 0),
        },
        "youju": _extra(final),
    }


async def _stream(conv_id: int, inp: RunInput, completion_id: str, created: int, model: str):
    def chunk(delta: dict, finish: str | None = None, extra: dict | None = None) -> str:
        data = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        if extra:
            data["youju"] = extra
        return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

    yield chunk({"role": "assistant", "content": ""})
    final: dict = {}
    live = True
    async for ev in run(inp):
        kind = ev["type"]
        if kind == "meta":
            # 智能体模式中途可能撤回文字，等最终结果出来再一次性发送
            live = ev.get("mode") != "agent"
        elif kind == "delta" and live:
            yield chunk({"content": ev["delta"]})
        elif kind == "reasoning" and live:
            yield chunk({"reasoning_content": ev["delta"]})
        elif kind == "final":
            final = ev
    if final:
        save_answer(conv_id, final=final)
        tail = references(final)
        if not live:
            tail = final.get("content", "") + tail
        if tail:
            yield chunk({"content": tail})
        yield chunk({}, "stop", _extra(final))
    yield "data: [DONE]\n\n"
