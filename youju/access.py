"""知识库是否可被当前调用者使用。

`visibility=team` 才是显式共享。团队智能体只表示这个智能体可被同事打开，
不会把创建者的私有库授权给其他账号。每次从数据库读可见性，不缓存授权结果。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from youju.deps import can_read
from youju.models import KnowledgeBase, User

HIDDEN_REPORT = "这份报告里的规程摘录已按当前知识库权限隐藏。"
HIDDEN_SOURCE = "来源已不可见"


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


def _kb_visible(db: Session, user: User | None, kb_id) -> bool:
    if user is None:
        return False
    if user.role == "admin":
        return True
    try:
        kb_id = int(kb_id)
    except (TypeError, ValueError):
        return False
    kb = db.get(KnowledgeBase, kb_id)
    return kb is not None and can_read(user, kb)


def _citations(sources) -> list[dict]:
    found = []
    for item in sources or []:
        if not isinstance(item, dict):
            found.append({"_invalid": True})
            continue
        if any(key in item for key in ("text", "chunk_id", "filename", "section", "kb_id")):
            found.append(item)
        found.extend(_citations(item.get("sources")))
        found.extend(_citations(item.get("claims")))
    return found


def knowledge_visible(db: Session, user: User | None, sources, *, report_present: bool) -> bool:
    """旧报告是否还能把规程原文交给这个用户。

    管理员保持可看。来源缺少知识库编号，或其中任一库当前不可读，则整段报告不能出示。
    不按词面猜测哪一句敏感。
    """
    if user is not None and user.role == "admin":
        return True
    citations = _citations(sources)
    if report_present and not citations:
        return False
    for item in citations:
        if item.get("_invalid") or not item.get("kb_id") or not _kb_visible(db, user, item.get("kb_id")):
            return False
    return True


def present_sources(db: Session, user: User | None, sources) -> list:
    shown = []
    for item in sources or []:
        shown.append(_present_source(db, user, item))
    return shown


def _present_source(db: Session, user: User | None, item) -> dict:
    if not isinstance(item, dict):
        return {"filename": HIDDEN_SOURCE, "section": "", "text": "", "redacted": True}
    nested = item.get("sources")
    nested_out = [_present_source(db, user, child) for child in nested] if isinstance(nested, list) else None
    claims = item.get("claims")
    claims_out = [_present_source(db, user, child) for child in claims] if isinstance(claims, list) else None
    blocked = bool(nested_out and any(child.get("redacted") for child in nested_out))
    blocked = blocked or bool(claims_out and any(child.get("redacted") for child in claims_out))
    is_citation = any(key in item for key in ("text", "chunk_id", "filename", "section", "kb_id"))
    if is_citation and (blocked or not item.get("kb_id") or not _kb_visible(db, user, item.get("kb_id"))):
        hidden = {
            "n": item.get("n"),
            "chunk_id": item.get("chunk_id"),
            "kb_id": item.get("kb_id"),
            "doc_id": item.get("doc_id"),
            "filename": HIDDEN_SOURCE,
            "section": "",
            "page": None,
            "text": "",
            "redacted": True,
        }
        if nested_out is not None:
            hidden["sources"] = nested_out
        if claims_out is not None:
            hidden["claims"] = []
        return hidden
    copied = dict(item)
    if nested_out is not None:
        copied["sources"] = nested_out
    if claims_out is not None:
        copied["claims"] = claims_out
    return copied


def present_check(check: dict | None, *, visible: bool) -> dict | None:
    if check is None:
        return None
    if visible or not isinstance(check, dict):
        return check
    return {
        "verdict": check.get("verdict") or "redacted",
        "total": check.get("total") or 0,
        "supported": check.get("supported") or 0,
        "weak": check.get("weak") or 0,
        "uncited": check.get("uncited") or 0,
        "invalid": check.get("invalid") or 0,
        "claims": [],
        "redacted": True,
    }


def present_report(report: str, *, visible: bool) -> str:
    if visible:
        return report or ""
    if not report:
        return ""
    return HIDDEN_REPORT
