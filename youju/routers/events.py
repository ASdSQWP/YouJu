"""事件中心：外部系统推送告警或工单，智能体按资料生成处置建议，再由人确认。"""

from __future__ import annotations

import json
import logging
from typing import Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from youju.agent.runner import AgentSnapshot, RunInput, collect
from youju.db import SessionLocal, get_db, utcnow
from youju.deps import can_edit, can_read, current_user, get_agent, visible
from youju.models import Agent, Conversation, Event, Message, User
from youju.routers.chat import short_title
from youju.seed import load_samples
from youju.serial import event_public

log = logging.getLogger("youju.events")

router = APIRouter(tags=["events"])

STATUSES = ("processing", "pending", "approved", "rejected", "failed")


class EventBody(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    level: str = Field(default="medium", max_length=16)
    source: str = Field(default="", max_length=120)
    data: Any = None


class SimulateBody(BaseModel):
    sample: str = Field(max_length=64)
    agent_id: int | None = None


class ReviewBody(BaseModel):
    action: Literal["approve", "reject"]
    comment: str = Field(default="", max_length=1000)


def _level(value: str) -> str:
    value = (value or "medium").strip()
    if len(value) == 2 and value[0] in "pP" and value[1].isdigit():
        return value.upper()
    return value.lower()


def _create(db: Session, agent: Agent, body: EventBody, user: User) -> Event:
    if not agent.allow_events:
        raise HTTPException(status_code=400, detail="这个智能体没有开启事件接入")
    ev = Event(
        agent_id=agent.id,
        title=body.title.strip(),
        level=_level(body.level),
        source=body.source.strip(),
        payload={"data": body.data},
        status="processing",
        created_by=user.id,
    )
    db.add(ev)
    db.commit()
    return ev


async def process_event(event_id: int) -> None:
    db = SessionLocal()
    try:
        ev = db.get(Event, event_id)
        agent = db.get(Agent, ev.agent_id) if ev else None
        if ev is None or agent is None:
            return
        snapshot = AgentSnapshot.of(agent)
        payload = {"title": ev.title, "level": ev.level, "source": ev.source, "data": (ev.payload or {}).get("data")}
    finally:
        db.close()
    try:
        final = await collect(RunInput(agent=snapshot, question=payload["title"], event=payload))
    except Exception as exc:  # noqa: BLE001
        log.exception("event %s failed", event_id)
        final = {"error": f"处理失败：{exc.__class__.__name__}"}
    db = SessionLocal()
    try:
        ev = db.get(Event, event_id)
        if ev is None:
            return
        model = final.get("model") or {}
        ev.report = final.get("content", "")
        ev.sources = final.get("sources", [])
        ev.verification = final.get("check")
        ev.trace = final.get("trace", [])
        ev.model_ref = model.get("ref")
        ev.model_label = model.get("label", "")
        ev.mode = final.get("mode", "")
        ev.usage = final.get("usage", {})
        ev.latency_ms = int(final.get("latency_ms") or 0)
        ev.refused = bool(final.get("refused"))
        ev.error = final.get("error")
        ev.status = "failed" if ev.error and not ev.report else "pending"
        db.commit()
    finally:
        db.close()


@router.post("/api/v1/agents/{agent_id}/events", status_code=202)
def push_event(
    agent_id: int,
    body: EventBody,
    background: BackgroundTasks,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    ev = _create(db, get_agent(db, agent_id, user), body, user)
    background.add_task(process_event, ev.id)
    return {"id": ev.id, "status": ev.status, "url": f"/#/events/{ev.id}"}


@router.get("/api/events/samples")
def samples(_user: User = Depends(current_user)):
    return {"samples": load_samples()}


@router.post("/api/events/simulate", status_code=202)
def simulate(
    body: SimulateBody,
    background: BackgroundTasks,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    sample = next((s for s in load_samples() if s["key"] == body.sample), None)
    if sample is None:
        raise HTTPException(status_code=404, detail="示例不存在")
    if body.agent_id:
        agent = get_agent(db, body.agent_id, user)
    else:
        agent = db.scalar(
            select(Agent).where(Agent.template == sample["template"], visible(Agent, user)).order_by(Agent.id)
        )
        if agent is None:
            raise HTTPException(status_code=404, detail="没有找到能处理这个示例的智能体")
    payload = EventBody(title=sample["title"], level=sample["level"], source=sample["source"], data=sample["data"])
    ev = _create(db, agent, payload, user)
    background.add_task(process_event, ev.id)
    return {"id": ev.id, "status": ev.status}


@router.get("/api/events")
def list_events(
    status: str | None = None,
    agent_id: int | None = None,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    agents = {a.id: a for a in db.scalars(select(Agent).where(visible(Agent, user))).all()}
    counts = {key: 0 for key in STATUSES}
    if not agents:
        return {"events": [], "counts": counts}
    query = select(Event).where(Event.agent_id.in_(list(agents))).order_by(Event.id.desc()).limit(200)
    if status:
        query = query.where(Event.status == status)
    if agent_id:
        query = query.where(Event.agent_id == agent_id)
    rows = db.scalars(query).all()
    for key, count in db.execute(
        select(Event.status, func.count(Event.id)).where(Event.agent_id.in_(list(agents))).group_by(Event.status)
    ):
        counts[key] = count
    return {"events": [event_public(e, agents.get(e.agent_id), brief=True) for e in rows], "counts": counts}


def _event(db: Session, event_id: int, user: User) -> tuple[Event, Agent]:
    ev = db.get(Event, event_id)
    agent = db.get(Agent, ev.agent_id) if ev else None
    if ev is None or agent is None or not can_read(user, agent):
        raise HTTPException(status_code=404, detail="事件不存在")
    return ev, agent


@router.get("/api/events/{event_id}")
def get_event(event_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    ev, agent = _event(db, event_id, user)
    return event_public(ev, agent)


@router.post("/api/events/{event_id}/review")
def review(event_id: int, body: ReviewBody, user: User = Depends(current_user), db: Session = Depends(get_db)):
    ev, agent = _event(db, event_id, user)
    if ev.status == "processing":
        raise HTTPException(status_code=400, detail="事件还在处理中")
    ev.status = "approved" if body.action == "approve" else "rejected"
    ev.review_comment = body.comment.strip()
    ev.reviewer_id = user.id
    ev.reviewed_at = utcnow()
    db.commit()
    return event_public(ev, agent)


@router.post("/api/events/{event_id}/conversation")
def follow_up(event_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    ev, agent = _event(db, event_id, user)
    if ev.status == "processing":
        raise HTTPException(status_code=400, detail="事件还在处理中")
    conv = Conversation(user_id=user.id, agent_id=agent.id, title=short_title(f"事件：{ev.title}"))
    db.add(conv)
    db.flush()
    data = json.dumps((ev.payload or {}).get("data"), ensure_ascii=False)
    db.add(Message(conv_id=conv.id, role="user", content=f"事件：{ev.title}\n级别：{ev.level}\n来源：{ev.source}\n数据：{data}"))
    db.add(
        Message(
            conv_id=conv.id,
            role="assistant",
            content=ev.report,
            sources=ev.sources,
            verification=ev.verification,
            trace=ev.trace,
            model_ref=ev.model_ref,
            model_label=ev.model_label,
            mode=ev.mode,
            usage=ev.usage,
            latency_ms=ev.latency_ms,
            refused=ev.refused,
        )
    )
    ev.conv_id = conv.id
    db.commit()
    return {"conversation_id": conv.id}


@router.delete("/api/events/{event_id}")
def delete_event(event_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    ev, agent = _event(db, event_id, user)
    if not can_edit(user, agent):
        raise HTTPException(status_code=403, detail="只有智能体的创建者或管理员可以删除事件")
    db.delete(ev)
    db.commit()
    return {"ok": True}
