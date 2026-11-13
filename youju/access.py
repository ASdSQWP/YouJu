"""知识库是否可被当前调用者使用。

`visibility=team` 才是显式共享。团队智能体只表示这个智能体可被同事打开，
不会把创建者的私有库授权给其他账号。每次从数据库读可见性，不缓存授权结果。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from youju.deps import can_read
from youju.models import KnowledgeBase, User


def load_bound_kbs(db: Session, kb_ids: list[int]) -> list[KnowledgeBase]:
    ids = []
    for kb_id in kb_ids or []:
        try:
            ids.append(int(kb_id))
        except (TypeError, ValueError):
            continue
    if not ids:
        return []
    rows = db.scalars(select(KnowledgeBase).where(KnowledgeBase.id.in_(ids))).all()
    by_id = {kb.id: kb for kb in rows}
    return [by_id[kb_id] for kb_id in ids if kb_id in by_id]


def readable_kbs(db: Session, user: User | None, kb_ids: list[int]) -> list[KnowledgeBase]:
    """当前用户自己能读的库，保持绑定顺序。"""
    if user is None:
        return []
    return [kb for kb in load_bound_kbs(db, kb_ids) if can_read(user, kb)]


def shared_agent_kbs(db: Session, owner_id: int, visibility: str, kb_ids: list[int]) -> list[KnowledgeBase]:
    """写进团队可见产物（例如事件报告）的库。

    团队智能体只保留 `visibility=team` 的库。私有智能体只有创建者和管理员能看到产物，
    因此沿用创建者本来就能读的库，不额外放开别人的私有库。
    """
    bound = load_bound_kbs(db, kb_ids)
    if (visibility or "team") == "team":
        return [kb for kb in bound if kb.visibility == "team"]
    owner = db.get(User, owner_id)
    if owner is None:
        return []
    return [kb for kb in bound if can_read(owner, kb)]
