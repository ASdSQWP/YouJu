"""登录态解析和资源权限。浏览器用 Cookie，外部系统用 Bearer API Key。"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from sqlalchemy import or_, select, true
from sqlalchemy.orm import Session

from youju.db import get_db, utcnow
from youju.models import Agent, ApiKey, KnowledgeBase, SessionToken, User
from youju.security import digest

COOKIE_NAME = "yj_session"


def _user_from_key(db: Session, key: str) -> User | None:
    if not key.startswith("yj-"):
        return None
    row = db.scalar(select(ApiKey).where(ApiKey.key_hash == digest(key), ApiKey.revoked.is_(False)))
    if row is None:
        return None
    user = db.get(User, row.user_id)
    if user is None or user.disabled:
        return None
    row.last_used_at = utcnow()
    db.commit()
    return user


def resolve_user(request: Request, db: Session) -> User | None:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return _user_from_key(db, header[7:].strip())
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    row = db.get(SessionToken, digest(token))
    if row is None or row.expires_at < utcnow():
        return None
    user = db.get(User, row.user_id)
    if user is None or user.disabled:
        return None
    return user


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    user = resolve_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="请先登录")
    return user


def admin_user(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def can_read(user: User, row) -> bool:
    return user.role == "admin" or row.owner_id == user.id or row.visibility == "team"


def can_edit(user: User, row) -> bool:
    return user.role == "admin" or row.owner_id == user.id


def visible(model, user: User):
    if user.role == "admin":
        return true()
    return or_(model.owner_id == user.id, model.visibility == "team")


def get_kb(db: Session, kb_id: int, user: User, write: bool = False) -> KnowledgeBase:
    kb = db.get(KnowledgeBase, kb_id)
    if kb is None or not can_read(user, kb):
        raise HTTPException(status_code=404, detail="知识库不存在")
    if write and not can_edit(user, kb):
        raise HTTPException(status_code=403, detail="只有创建者或管理员可以修改这个知识库")
    return kb


def get_agent(db: Session, agent_id: int, user: User, write: bool = False) -> Agent:
    agent = db.get(Agent, agent_id)
    if agent is None or not can_read(user, agent):
        raise HTTPException(status_code=404, detail="智能体不存在")
    if write and not can_edit(user, agent):
        raise HTTPException(status_code=403, detail="只有创建者或管理员可以修改这个智能体")
    return agent
