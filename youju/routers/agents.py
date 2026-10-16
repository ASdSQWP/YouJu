"""智能体配置。"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from youju.db import get_db, iso
from youju.deps import can_edit, can_read, current_user, get_agent, visible
from youju.models import Agent, KnowledgeBase, User
from youju.providers.registry import LOCAL_CHAT, find_model
from youju.seed.templates import PACKS, TEMPLATES

PACK_NAMES = {pack["key"]: pack["name"] for pack in PACKS}

router = APIRouter(tags=["agents"])


class ModelBindBody(BaseModel):
    model_ref: str | None = Field(default=None, max_length=160)


class AgentBody(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    emoji: str = Field(default="✨", min_length=1, max_length=8)
    color: str = Field(default="#0f766e", pattern=r"^#[0-9a-fA-F]{6}$")
    category: str = Field(default="通用", max_length=16)
    description: str = Field(default="", max_length=300)
    system_prompt: str = Field(default="", max_length=6000)
    opening: str = Field(default="", max_length=500)
    suggestions: list[str] = Field(default_factory=list, max_length=6)
    kb_ids: list[int] = Field(default_factory=list, max_length=20)
    model_ref: str | None = Field(default=None, max_length=160)
    strategy: Literal["rag", "agent"] = "rag"
    strict: bool = True
    strictness: Literal["loose", "standard", "strict"] = "standard"
    top_k: int = Field(default=5, ge=1, le=12)
    temperature: float = Field(default=0.3, ge=0, le=1.5)
    allow_events: bool = False
    visibility: Literal["private", "team"] = "team"
    template: str = Field(default="", max_length=32)

    @field_validator("suggestions")
    @classmethod
    def _clean(cls, items: list[str]) -> list[str]:
        return [item.strip()[:80] for item in items if item and item.strip()]


def agent_public(db: Session, agent: Agent, user: User, kb_names: dict[int, str] | None = None) -> dict:
    if kb_names is None:
        rows = db.execute(select(KnowledgeBase.id, KnowledgeBase.name).where(KnowledgeBase.id.in_(agent.kb_ids or [-1])))
        kb_names = {kid: name for kid, name in rows}
    model_label = None
    if agent.model_ref:
        _, info = find_model(db, agent.model_ref)
        model_label = info.label if info else f"{agent.model_ref}（不可用）"
    owner = db.get(User, agent.owner_id)
    return {
        "id": agent.id,
        "name": agent.name,
        "emoji": agent.emoji,
        "color": agent.color,
        "category": agent.category,
        "description": agent.description,
        "system_prompt": agent.system_prompt,
        "opening": agent.opening,
        "suggestions": agent.suggestions or [],
        "kb_ids": [k for k in agent.kb_ids or [] if k in kb_names],
        "kbs": [{"id": k, "name": kb_names[k]} for k in agent.kb_ids or [] if k in kb_names],
        "model_ref": agent.model_ref,
        "model_label": model_label,
        "strategy": agent.strategy,
        "strict": agent.strict,
        "strictness": agent.strictness,
        "top_k": agent.top_k,
        "temperature": agent.temperature,
        "allow_events": agent.allow_events,
        "visibility": agent.visibility,
        "template": agent.template,
        "owner_id": agent.owner_id,
        "owner_name": owner.display_name if owner else "",
        "can_edit": can_edit(user, agent),
        "created_at": iso(agent.created_at),
        "updated_at": iso(agent.updated_at),
    }


def _validate(db: Session, body: AgentBody, user: User) -> None:
    for kb_id in body.kb_ids:
        kb = db.get(KnowledgeBase, kb_id)
        if kb is None or not can_read(user, kb):
            raise HTTPException(status_code=400, detail=f"知识库 {kb_id} 不存在或没有权限")
    if body.model_ref and body.model_ref != LOCAL_CHAT:
        _, info = find_model(db, body.model_ref)
        if info is None or info.type != "chat":
            raise HTTPException(status_code=400, detail="选择的对话模型不可用")


def _apply(agent: Agent, body: AgentBody) -> None:
    for field in (
        "name", "emoji", "color", "category", "description", "system_prompt", "opening", "suggestions",
        "model_ref", "strategy", "strict", "strictness", "top_k", "temperature", "allow_events", "visibility", "template",
    ):
        setattr(agent, field, getattr(body, field))
    agent.name = body.name.strip()
    agent.kb_ids = list(dict.fromkeys(body.kb_ids))
    agent.model_ref = body.model_ref or None


@router.get("/api/agents")
def list_agents(user: User = Depends(current_user), db: Session = Depends(get_db)):
    agents = db.scalars(select(Agent).where(visible(Agent, user)).order_by(Agent.id)).all()
    kb_names = {kid: name for kid, name in db.execute(select(KnowledgeBase.id, KnowledgeBase.name))}
    return {"agents": [agent_public(db, agent, user, kb_names) for agent in agents]}


@router.get("/api/agents/templates")
def templates(_user: User = Depends(current_user)):
    return {"templates": [{**t, "pack_name": PACK_NAMES.get(t.get("pack") or "")} for t in TEMPLATES]}


@router.post("/api/agents")
def create_agent(body: AgentBody, user: User = Depends(current_user), db: Session = Depends(get_db)):
    _validate(db, body, user)
    agent = Agent(owner_id=user.id)
    _apply(agent, body)
    db.add(agent)
    db.commit()
    return agent_public(db, agent, user)


@router.get("/api/agents/{agent_id}")
def get_one(agent_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return agent_public(db, get_agent(db, agent_id, user), user)


@router.patch("/api/agents/{agent_id}/model")
def bind_agent_model(agent_id: int, body: ModelBindBody, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """把对话里选中的大模型写回这个智能体。"""
    agent = get_agent(db, agent_id, user, write=True)
    ref = (body.model_ref or "").strip() or None
    if ref and ref != LOCAL_CHAT:
        _, info = find_model(db, ref)
        if info is None or info.type != "chat":
            raise HTTPException(status_code=400, detail="选择的对话模型不可用")
    agent.model_ref = None if ref in (None, LOCAL_CHAT) else ref
    db.commit()
    return agent_public(db, agent, user)


@router.put("/api/agents/{agent_id}")
def update_agent(agent_id: int, body: AgentBody, user: User = Depends(current_user), db: Session = Depends(get_db)):
    agent = get_agent(db, agent_id, user, write=True)
    _validate(db, body, user)
    _apply(agent, body)
    db.commit()
    return agent_public(db, agent, user)


@router.delete("/api/agents/{agent_id}")
def delete_agent(agent_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    agent = get_agent(db, agent_id, user, write=True)
    db.delete(agent)
    db.commit()
    return {"ok": True}
