"""运营看板：用量、拒答、核验结果、差评和知识缺口。"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from youju.db import get_db, iso, utcnow
from youju.deps import admin_user
from youju.models import Agent, Chunk, Conversation, Document, Event, KnowledgeBase, Message, User

router = APIRouter(tags=["stats"])


def _local_day(value: datetime) -> str:
    return value.replace(tzinfo=timezone.utc).astimezone().date().isoformat()


@router.get("/api/stats")
def stats(days: int = 14, _admin: User = Depends(admin_user), db: Session = Depends(get_db)):
    days = max(1, min(days, 90))

    def count(model, *where) -> int:
        return int(db.scalar(select(func.count()).select_from(model).where(*where)) or 0)

    totals = {
        "users": count(User),
        "agents": count(Agent),
        "kbs": count(KnowledgeBase),
        "documents": count(Document, Document.status == "ready"),
        "chunks": count(Chunk),
        "conversations": count(Conversation),
        "answers": count(Message, Message.role == "assistant"),
        "events": count(Event),
    }
    rows = db.execute(
        select(
            Message.id, Message.conv_id, Message.role, Message.content, Message.created_at, Message.refused,
            Message.model_ref, Message.model_label, Message.usage, Message.latency_ms, Message.feedback,
            Message.feedback_note, Message.verification, Message.error,
        )
        .where(Message.created_at >= utcnow() - timedelta(days=days))
        .order_by(Message.id)
    ).all()
    conv_ids = {r.conv_id for r in rows} or {-1}
    conv_agent = {c.id: c.agent_id for c in db.scalars(select(Conversation).where(Conversation.id.in_(conv_ids)))}
    agents = {a.id: a for a in db.scalars(select(Agent)).all()}

    today = datetime.now().date()
    daily = {}
    for offset in range(days - 1, -1, -1):
        day = (today - timedelta(days=offset)).isoformat()
        daily[day] = {"date": day, "answers": 0, "refused": 0}
    models: dict[str, dict] = {}
    checks: Counter = Counter()
    feedback = {"up": 0, "down": 0}
    last_question: dict[int, str] = {}
    gaps: dict[str, dict] = {}
    negative: list[dict] = []
    latencies: list[int] = []

    for r in rows:
        if r.role == "user":
            last_question[r.conv_id] = r.content
            continue
        agent = agents.get(conv_agent.get(r.conv_id))
        day = _local_day(r.created_at)
        if day in daily:
            daily[day]["answers"] += 1
            if r.refused:
                daily[day]["refused"] += 1
        key = r.model_ref or "unknown"
        item = models.setdefault(key, {"ref": key, "label": r.model_label or key, "answers": 0, "input": 0, "output": 0, "latency": 0, "errors": 0})
        usage = r.usage or {}
        item["answers"] += 1
        item["input"] += int(usage.get("input") or 0)
        item["output"] += int(usage.get("output") or 0)
        item["latency"] += int(r.latency_ms or 0)
        item["errors"] += 1 if r.error else 0
        latencies.append(int(r.latency_ms or 0))
        checks[(r.verification or {}).get("verdict") or "none"] += 1
        question = last_question.get(r.conv_id, "").strip()
        if r.feedback == 1:
            feedback["up"] += 1
        elif r.feedback == -1:
            feedback["down"] += 1
            negative.append(
                {
                    "message_id": r.id,
                    "question": question,
                    "answer": (r.content or "")[:200],
                    "note": r.feedback_note,
                    "agent": agent.name if agent else "",
                    "kb_ids": list(agent.kb_ids or []) if agent else [],
                    "created_at": iso(r.created_at),
                }
            )
        if r.refused and question:
            norm = re.sub(r"[\s\W_]+", "", question.lower())[:60]
            gap = gaps.setdefault(
                norm,
                {
                    "question": question,
                    "count": 0,
                    "agent": agent.name if agent else "",
                    "kb_ids": list(agent.kb_ids or []) if agent else [],
                    "last_at": None,
                },
            )
            gap["count"] += 1
            gap["last_at"] = iso(r.created_at)

    answers = sum(m["answers"] for m in models.values())
    refused = sum(d["refused"] for d in daily.values())
    judged = sum(v for k, v in checks.items() if k != "none")
    model_rows = [
        {**m, "avg_latency_ms": int(m["latency"] / m["answers"]) if m["answers"] else 0} for m in models.values()
    ]
    return {
        "days": days,
        "totals": totals,
        "daily": list(daily.values()),
        "models": sorted(model_rows, key=lambda m: -m["answers"]),
        "checks": dict(checks),
        "feedback": feedback,
        "gaps": sorted(gaps.values(), key=lambda g: (-g["count"], g["last_at"] or ""))[:12],
        "negative": list(reversed(negative))[:12],
        "events": dict(db.execute(select(Event.status, func.count(Event.id)).group_by(Event.status)).all()),
        "summary": {
            "answers": answers,
            "refusal_rate": round(refused / answers, 4) if answers else 0.0,
            "pass_rate": round(checks.get("pass", 0) / judged, 4) if judged else 0.0,
            "avg_latency_ms": int(sum(latencies) / len(latencies)) if latencies else 0,
        },
    }
