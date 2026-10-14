"""API Key：给外部系统调用 OpenAI 兼容接口、事件接入和 MCP 用。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from youju.db import get_db, iso
from youju.deps import current_user
from youju.models import ApiKey, User
from youju.security import digest, new_api_key

router = APIRouter(tags=["apikeys"])


class KeyBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)


def key_public(row: ApiKey) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "prefix": row.prefix,
        "created_at": iso(row.created_at),
        "last_used_at": iso(row.last_used_at),
    }


@router.get("/api/apikeys")
def list_keys(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.scalars(
        select(ApiKey).where(ApiKey.user_id == user.id, ApiKey.revoked.is_(False)).order_by(ApiKey.id.desc())
    ).all()
    return {"keys": [key_public(r) for r in rows]}


@router.post("/api/apikeys")
def create_key(body: KeyBody, user: User = Depends(current_user), db: Session = Depends(get_db)):
    key = new_api_key()
    row = ApiKey(user_id=user.id, name=body.name.strip(), prefix=key[:10], key_hash=digest(key))
    db.add(row)
    db.commit()
    return {**key_public(row), "key": key}


@router.delete("/api/apikeys/{key_id}")
def revoke_key(key_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    row = db.get(ApiKey, key_id)
    if row is None or (row.user_id != user.id and user.role != "admin"):
        raise HTTPException(status_code=404, detail="密钥不存在")
    row.revoked = True
    db.commit()
    return {"ok": True}
