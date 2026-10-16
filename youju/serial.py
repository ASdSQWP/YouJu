"""ORM 对象转成接口返回的字典。"""

from __future__ import annotations

from youju.db import iso


def user_public(user) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "role": user.role,
        "disabled": user.disabled,
        "created_at": iso(user.created_at),
        "last_login_at": iso(user.last_login_at),
    }


def doc_public(doc) -> dict:
    return {
        "id": doc.id,
        "kb_id": doc.kb_id,
        "filename": doc.filename,
        "ext": doc.ext,
        "size": doc.size,
        "status": doc.status,
        "error": doc.error,
        "chunk_count": doc.chunk_count,
        "char_count": doc.char_count,
        "created_at": iso(doc.created_at),
        "updated_at": iso(doc.updated_at),
    }


def message_public(msg) -> dict:
    return {
        "id": msg.id,
        "role": msg.role,
        "content": msg.content,
        "reasoning": msg.reasoning or "",
        "sources": msg.sources or [],
        "check": msg.verification,
        "trace": msg.trace or [],
        "model_ref": msg.model_ref,
        "model_label": msg.model_label,
        "mode": msg.mode,
        "usage": msg.usage or {},
        "latency_ms": msg.latency_ms,
        "refused": msg.refused,
        "stopped": msg.stopped,
        "error": msg.error,
        "feedback": msg.feedback,
        "feedback_note": msg.feedback_note,
        "attachments": list(getattr(msg, "attachments", None) or []),
        "created_at": iso(msg.created_at),
    }


def event_public(ev, agent=None, brief: bool = False) -> dict:
    data = {
        "id": ev.id,
        "agent_id": ev.agent_id,
        "agent_name": agent.name if agent else "",
        "agent_emoji": agent.emoji if agent else "",
        "agent_color": agent.color if agent else "",
        "title": ev.title,
        "level": ev.level,
        "source": ev.source,
        "status": ev.status,
        "refused": ev.refused,
        "check": ev.verification,
        "model_label": ev.model_label,
        "created_at": iso(ev.created_at),
        "updated_at": iso(ev.updated_at),
        "reviewed_at": iso(ev.reviewed_at),
    }
    if not brief:
        data.update(
            {
                "payload": ev.payload or {},
                "report": ev.report,
                "sources": ev.sources or [],
                "trace": ev.trace or [],
                "mode": ev.mode,
                "usage": ev.usage or {},
                "latency_ms": ev.latency_ms,
                "error": ev.error,
                "review_comment": ev.review_comment,
                "reviewer_id": ev.reviewer_id,
                "conv_id": ev.conv_id,
            }
        )
    return data


def source_public(src, boxes=None, tick=0, owner=None, viewer=None) -> dict:
    extra = src.extra or {}
    can_edit = False
    if viewer is not None:
        can_edit = viewer.role == "admin" or src.owner_id == viewer.id
    return {
        "id": src.id,
        "owner_id": src.owner_id,
        "owner_name": (owner.display_name if owner else "") or (owner.username if owner else ""),
        "name": src.name,
        "code": src.code,
        "kind": src.kind,
        "uri": src.uri,
        "scenario": src.scenario,
        "category": getattr(src, "category", None) or "other",
        "visibility": getattr(src, "visibility", None) or "private",
        "shared_user_ids": list(getattr(src, "shared_user_ids", None) or []),
        "folder_id": getattr(src, "folder_id", None),
        "can_edit": can_edit,
        "status": src.status,
        "filename": src.filename,
        "mime": src.mime,
        "live": src.status == "live",
        "tick": tick,
        "boxes": boxes or [],
        "last_error": src.last_error,
        "live_started_at": iso(src.live_started_at),
        "created_at": iso(src.created_at),
        "media_url": f"/api/vision/sources/{src.id}/media" if src.kind == "file" else "",
        "frame_url": f"/api/vision/sources/{src.id}/frame",
        "preview_kind": extra.get("preview") or ("video" if (src.mime or "").startswith("video/") else "image" if src.kind == "file" else "demo"),
        "vlm_skipped": bool(extra.get("vlm_skipped")),
        "vlm_hint": extra.get("vlm_hint") or "",
        "vlm_analyzed": bool(extra.get("vlm_analyzed")),
    }


def rule_public(rule, source=None, agent=None) -> dict:
    return {
        "id": rule.id,
        "name": rule.name,
        "instruction": rule.instruction,
        "source_id": rule.source_id,
        "source_name": source.name if source else "",
        "algorithms": list(rule.algorithms or []),
        "confidence": rule.confidence,
        "cooldown_sec": rule.cooldown_sec,
        "level": rule.level,
        "detector": rule.detector,
        "small_model": getattr(rule, "small_model", None) or "demo",
        "vlm_model": getattr(rule, "vlm_model", None) or "",
        "agent_id": rule.agent_id,
        "agent_name": agent.name if agent else "",
        "agent_emoji": agent.emoji if agent else "",
        "enabled": rule.enabled,
        "auto_push": rule.auto_push,
        "auto_pull": rule.auto_pull,
        "created_at": iso(rule.created_at),
        "updated_at": iso(rule.updated_at),
    }


def alert_public(alert, source=None, rule=None) -> dict:
    has_snap = bool(getattr(alert, "snapshot_path", None))
    return {
        "id": alert.id,
        "source_id": alert.source_id,
        "source_name": source.name if source else "",
        "source_code": source.code if source else "",
        "rule_id": alert.rule_id,
        "rule_name": rule.name if rule else "",
        "title": alert.title,
        "level": alert.level,
        "findings": list(alert.findings or []),
        "boxes": list(alert.boxes or []),
        "confidence": alert.confidence,
        "detector": alert.detector,
        "tick": alert.tick,
        "evidence": list(getattr(alert, "evidence", None) or []),
        "has_snapshot": has_snap,
        "snapshot_url": f"/api/vision/alerts/{alert.id}/snapshot" if has_snap else "",
        "event_id": alert.event_id,
        "status": alert.status,
        "created_at": iso(alert.created_at),
    }
