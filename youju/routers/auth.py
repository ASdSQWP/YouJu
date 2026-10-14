"""初始化、登录和会话。"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from youju import __version__
from youju.config import settings
from youju.db import get_db, utcnow
from youju.deps import COOKIE_NAME, resolve_user
from youju.models import SessionToken, User
from youju.security import digest, hash_password, login_limiter, new_token, verify_password
from youju.seed import seed_demo
from youju.vision.seed import seed_vision
from youju.serial import user_public

router = APIRouter(prefix="/api/auth", tags=["auth"])

USERNAME = r"^[A-Za-z0-9_.@-]{2,32}$"


class SetupBody(BaseModel):
    username: str = Field(pattern=USERNAME)
    password: str = Field(min_length=6, max_length=128)
    display_name: str = Field(default="", max_length=32)
    seed_demo: bool = True


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


def open_session(db: Session, response: Response, user: User) -> None:
    token = new_token()
    db.add(
        SessionToken(
            token_hash=digest(token),
            user_id=user.id,
            expires_at=utcnow() + timedelta(days=settings.session_days),
        )
    )
    user.last_login_at = utcnow()
    db.commit()
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=settings.session_days * 86400,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )


@router.get("/status")
def status(request: Request, db: Session = Depends(get_db)):
    initialized = bool(db.scalar(select(func.count(User.id))))
    user = resolve_user(request, db) if initialized else None
    return {"initialized": initialized, "user": user_public(user) if user else None, "version": __version__}


@router.post("/setup")
def setup(body: SetupBody, response: Response, db: Session = Depends(get_db)):
    if db.scalar(select(func.count(User.id))):
        raise HTTPException(status_code=409, detail="系统已经初始化，请直接登录")
    user = User(
        username=body.username,
        display_name=body.display_name.strip() or body.username,
        password_hash=hash_password(body.password),
        role="admin",
    )
    db.add(user)
    db.commit()
    if body.seed_demo:
        seed_demo(db, user)
        seed_vision(db, user)
    open_session(db, response, user)
    return {"user": user_public(user)}


@router.post("/login")
def login(body: LoginBody, response: Response, db: Session = Depends(get_db)):
    key = body.username.strip().lower()
    if login_limiter.blocked(key):
        raise HTTPException(status_code=429, detail="失败次数过多，请 5 分钟后再试")
    user = db.scalar(select(User).where(User.username == body.username.strip()))
    if user is None or not verify_password(body.password, user.password_hash):
        login_limiter.fail(key)
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    if user.disabled:
        raise HTTPException(status_code=403, detail="账号已停用，请联系管理员")
    login_limiter.reset(key)
    open_session(db, response, user)
    return {"user": user_public(user)}


@router.post("/logout")
def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    token = request.cookies.get(COOKIE_NAME)
    if token:
        row = db.get(SessionToken, digest(token))
        if row is not None:
            db.delete(row)
            db.commit()
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}
