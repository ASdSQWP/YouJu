"""个人资料和成员管理。"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from youju.db import get_db
from youju.deps import admin_user, current_user
from youju.models import SessionToken, User
from youju.routers.auth import USERNAME
from youju.security import hash_password, verify_password
from youju.serial import user_public

router = APIRouter(tags=["users"])


class ProfileBody(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=32)
    old_password: str | None = Field(default=None, max_length=128)
    new_password: str | None = Field(default=None, min_length=6, max_length=128)


class NewUserBody(BaseModel):
    username: str = Field(pattern=USERNAME)
    display_name: str = Field(default="", max_length=32)
    password: str = Field(min_length=6, max_length=128)
    role: Literal["admin", "member"] = "member"


class UserPatch(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=32)
    role: Literal["admin", "member"] | None = None
    disabled: bool | None = None
    password: str | None = Field(default=None, min_length=6, max_length=128)


@router.get("/api/me")
def me(user: User = Depends(current_user)):
    return user_public(user)


@router.patch("/api/me")
def update_me(body: ProfileBody, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if body.display_name:
        user.display_name = body.display_name.strip()
    if body.new_password:
        if not body.old_password or not verify_password(body.old_password, user.password_hash):
            raise HTTPException(status_code=400, detail="原密码不正确")
        user.password_hash = hash_password(body.new_password)
    db.commit()
    return user_public(user)


@router.get("/api/users/directory")
def user_directory(_user: User = Depends(current_user), db: Session = Depends(get_db)):
    """登录用户可选人共享媒体；不含密码与敏感字段。"""
    rows = db.scalars(select(User).where(User.disabled.is_(False)).order_by(User.id)).all()
    return {
        "users": [
            {"id": u.id, "username": u.username, "display_name": u.display_name, "role": u.role}
            for u in rows
        ]
    }


@router.get("/api/users")
def list_users(_admin: User = Depends(admin_user), db: Session = Depends(get_db)):
    return {"users": [user_public(u) for u in db.scalars(select(User).order_by(User.id)).all()]}


@router.post("/api/users")
def create_user(body: NewUserBody, _admin: User = Depends(admin_user), db: Session = Depends(get_db)):
    if db.scalar(select(User.id).where(User.username == body.username)):
        raise HTTPException(status_code=409, detail="用户名已存在")
    user = User(
        username=body.username,
        display_name=body.display_name.strip() or body.username,
        password_hash=hash_password(body.password),
        role=body.role,
    )
    db.add(user)
    db.commit()
    return user_public(user)


@router.patch("/api/users/{user_id}")
def update_user(user_id: int, body: UserPatch, admin: User = Depends(admin_user), db: Session = Depends(get_db)):
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    if user.id == admin.id and (body.disabled or body.role == "member"):
        raise HTTPException(status_code=400, detail="不能停用自己或取消自己的管理员身份")
    losing_admin = user.role == "admin" and (body.role == "member" or body.disabled)
    if losing_admin:
        active_admins = db.scalar(select(func.count(User.id)).where(User.role == "admin", User.disabled.is_(False)))
        if (active_admins or 0) <= 1:
            raise HTTPException(status_code=400, detail="至少需要保留一个可用的管理员")
    if body.display_name:
        user.display_name = body.display_name.strip()
    if body.role:
        user.role = body.role
    if body.disabled is not None:
        user.disabled = body.disabled
    if body.password:
        user.password_hash = hash_password(body.password)
    if body.disabled or body.password:
        db.execute(delete(SessionToken).where(SessionToken.user_id == user.id))
    db.commit()
    return user_public(user)
