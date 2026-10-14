"""启动时的收尾：中断的任务标记失败；配置了 YOUJU_ADMIN_PASSWORD 时自动初始化。"""

from __future__ import annotations

from sqlalchemy import func, select

from youju.config import settings
from youju.db import SessionLocal
from youju.models import Document, Event, User
from youju.security import hash_password
from youju.seed import seed_demo


def bootstrap() -> None:
    db = SessionLocal()
    try:
        for doc in db.scalars(select(Document).where(Document.status.in_(("pending", "processing")))).all():
            doc.status = "failed"
            doc.error = "服务重启时任务中断，请点击重试"
        for ev in db.scalars(select(Event).where(Event.status == "processing")).all():
            ev.status = "failed"
            ev.error = "服务重启时任务中断"
        db.commit()
        if settings.admin_password and not db.scalar(select(func.count(User.id))):
            admin = User(
                username="admin",
                display_name="管理员",
                password_hash=hash_password(settings.admin_password),
                role="admin",
            )
            db.add(admin)
            db.commit()
            seed_demo(db, admin)
    finally:
        db.close()
