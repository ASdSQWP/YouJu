"""对话列表和流式回答。"""

from __future__ import annotations

import json
from datetime import datetime
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from youju.agent.citations import used_numbers
from youju.agent.runner import AgentSnapshot, RunInput, run
from youju.answers import new_state, save_answer, track
from youju.db import get_db, iso, utcnow
from youju.deps import can_read, current_user
from youju.models import Agent, Conversation, Message, User
from youju.serial import message_public

router = APIRouter(tags=["chat"])

SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}
# 系统代理常按 2KB 缓冲，先垫一行 SSE 注释才能立刻看到打字过程
SSE_PAD = ":" + (" " * 2048) + "\n\n"


def sse(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


class ConvBody(BaseModel):
    agent_id: int
    title: str | None = Field(default=None, max_length=120)


class ConvPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=120)
    pinned: bool | None = None


class ChatBody(BaseModel):
    content: str | None = Field(default=None, max_length=8000)
    model: str | None = Field(default=None, max_length=160)
    regenerate: bool = False


class FeedbackBody(BaseModel):
    rating: int = Field(ge=-1, le=1)
    note: str = Field(default="", max_length=500)


def short_title(text: str) -> str:
    line = " ".join(text.split())
    return line[:28] + ("…" if len(line) > 28 else "")


def conv_public(conv: Conversation, agent: Agent | None) -> dict:
    return {
        "id": conv.id,
        "title": conv.title,
        "agent_id": conv.agent_id,
        "agent_name": agent.name if agent else "已删除的智能体",
        "agent_emoji": agent.emoji if agent else "🗑️",
        "agent_color": agent.color if agent else "#94a3b8",
        "pinned": conv.pinned,
        "source": conv.source,
        "created_at": iso(conv.created_at),
        "updated_at": iso(conv.updated_at),
    }


def _own(db: Session, conv_id: int, user: User) -> Conversation:
    conv = db.get(Conversation, conv_id)
    if conv is None or conv.user_id != user.id:
        raise HTTPException(status_code=404, detail="对话不存在")
    return conv


@router.get("/api/conversations")
def list_conversations(user: User = Depends(current_user), db: Session = Depends(get_db)):
    convs = db.scalars(
        select(Conversation)
        .where(Conversation.user_id == user.id, Conversation.source == "web")
        .order_by(Conversation.pinned.desc(), Conversation.updated_at.desc())
        .limit(200)
    ).all()
    ids = {c.agent_id for c in convs if c.agent_id} or {-1}
    agents = {a.id: a for a in db.scalars(select(Agent).where(Agent.id.in_(ids))).all()}
    return {"conversations": [conv_public(c, agents.get(c.agent_id)) for c in convs]}


@router.post("/api/conversations")
def create_conversation(body: ConvBody, user: User = Depends(current_user), db: Session = Depends(get_db)):
    agent = db.get(Agent, body.agent_id)
    if agent is None or not can_read(user, agent):
        raise HTTPException(status_code=404, detail="智能体不存在")
    conv = Conversation(user_id=user.id, agent_id=agent.id, title=(body.title or "").strip() or "新对话")
    db.add(conv)
    db.commit()
    return conv_public(conv, agent)


@router.get("/api/conversations/{conv_id}")
def get_conversation(conv_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    conv = _own(db, conv_id, user)
    agent = db.get(Agent, conv.agent_id) if conv.agent_id else None
    msgs = db.scalars(select(Message).where(Message.conv_id == conv.id).order_by(Message.id)).all()
    return {"conversation": conv_public(conv, agent), "messages": [message_public(m) for m in msgs]}


@router.patch("/api/conversations/{conv_id}")
def update_conversation(conv_id: int, body: ConvPatch, user: User = Depends(current_user), db: Session = Depends(get_db)):
    conv = _own(db, conv_id, user)
    if body.title is not None:
        conv.title = body.title.strip()
    if body.pinned is not None:
        conv.pinned = body.pinned
    db.commit()
    agent = db.get(Agent, conv.agent_id) if conv.agent_id else None
    return conv_public(conv, agent)


@router.delete("/api/conversations/{conv_id}")
def delete_conversation(conv_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    conv = _own(db, conv_id, user)
    db.execute(delete(Message).where(Message.conv_id == conv.id))
    db.delete(conv)
    db.commit()
    return {"ok": True}


@router.post("/api/conversations/{conv_id}/chat")
def chat(conv_id: int, body: ChatBody, user: User = Depends(current_user), db: Session = Depends(get_db)):
    conv = _own(db, conv_id, user)
    agent = db.get(Agent, conv.agent_id) if conv.agent_id else None
    if agent is None or not can_read(user, agent):
        raise HTTPException(status_code=404, detail="智能体已删除或没有权限")
    msgs = list(db.scalars(select(Message).where(Message.conv_id == conv.id).order_by(Message.id)).all())
    if body.regenerate:
        while msgs and msgs[-1].role == "assistant":
            db.delete(msgs.pop())
        if not msgs:
            raise HTTPException(status_code=400, detail="没有可以重新生成的问题")
        question = msgs[-1]
        history = msgs[:-1]
    else:
        content = (body.content or "").strip()
        if not content:
            raise HTTPException(status_code=400, detail="消息不能为空")
        question = Message(conv_id=conv.id, role="user", content=content)
        db.add(question)
        history = msgs
        if not msgs and conv.title in ("", "新对话"):
            conv.title = short_title(content)
    conv.updated_at = utcnow()
    db.commit()
    inp = RunInput(
        agent=AgentSnapshot.of(agent),
        question=question.content,
        history=[{"role": m.role, "content": m.content} for m in history if m.role in ("user", "assistant")],
        model_ref=body.model,
    )
    start = {"type": "start", "conversation_id": conv.id, "user_message_id": question.id, "title": conv.title}
    db.commit()
    db.close()
    return StreamingResponse(
        stream_answer(conv.id, inp, start),
        media_type="text/event-stream; charset=utf-8",
        headers=SSE_HEADERS,
    )


async def stream_answer(conv_id: int, inp: RunInput, start: dict):
    state = new_state()
    saved = False
    try:
        yield SSE_PAD
        yield sse(start)
        async for ev in run(inp):
            if ev["type"] == "final":
                ev = {**ev, "message_id": save_answer(conv_id, final=ev)}
                saved = True
            else:
                track(state, ev)
            yield sse(ev)
        yield sse({"type": "done"})
    finally:
        if not saved:
            save_answer(conv_id, partial=state)


@router.post("/api/messages/{message_id}/feedback")
def feedback(message_id: int, body: FeedbackBody, user: User = Depends(current_user), db: Session = Depends(get_db)):
    msg = db.get(Message, message_id)
    if msg is None or msg.role != "assistant":
        raise HTTPException(status_code=404, detail="消息不存在")
    _own(db, msg.conv_id, user)
    msg.feedback = body.rating
    msg.feedback_note = body.note.strip()
    db.commit()
    return {"ok": True, "feedback": msg.feedback}


@router.get("/api/conversations/{conv_id}/export")
def export(conv_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    conv = _own(db, conv_id, user)
    agent = db.get(Agent, conv.agent_id) if conv.agent_id else None
    msgs = db.scalars(select(Message).where(Message.conv_id == conv.id).order_by(Message.id)).all()
    lines = [f"# {conv.title}", "", f"> 智能体：{agent.name if agent else '-'}　导出时间：{datetime.now():%Y-%m-%d %H:%M}", ""]
    for msg in msgs:
        if msg.role == "user":
            lines += ["## 提问", "", msg.content, ""]
            continue
        lines += ["## 回答", "", msg.content, ""]
        used = used_numbers(msg.content)
        refs = [s for s in msg.sources or [] if s.get("n") in used]
        if refs:
            lines += ["**参考资料**", ""]
            lines += [f"- [{s['n']}] {s['filename']}" + (f" · {s['section']}" if s.get("section") else "") for s in refs]
            lines.append("")
    filename = quote(f"{conv.title or '对话'}.md")
    return PlainTextResponse(
        "\n".join(lines),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )
