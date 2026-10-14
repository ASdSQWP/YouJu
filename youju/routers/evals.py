"""检索评测：每个知识库一组问题和期望文档，算 Hit@1、Hit@K、MRR。"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from youju.db import get_db, iso
from youju.deps import current_user, get_kb
from youju.models import Conversation, EvalCase, EvalRun, Message, User
from youju.rag.retrieve import retrieve

router = APIRouter(tags=["eval"])


class CaseBody(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    expected: str = Field(min_length=1, max_length=255)


class RunBody(BaseModel):
    top_k: int = Field(default=3, ge=1, le=10)


class FromMessageBody(BaseModel):
    kb_id: int
    expected: str = Field(min_length=1, max_length=255)


def case_public(case: EvalCase) -> dict:
    return {"id": case.id, "question": case.question, "expected": case.expected, "created_at": iso(case.created_at)}


def run_public(run: EvalRun, details: bool) -> dict:
    data = {"id": run.id, "top_k": run.top_k, "metrics": run.metrics, "created_at": iso(run.created_at)}
    if details:
        data["details"] = run.details
    return data


def evaluate(kb_id: int, cases: list[tuple[int, str, str]], top_k: int) -> list[dict]:
    rows = []
    for case_id, question, expected in cases:
        result = retrieve([kb_id], question, top_k=top_k)
        files = [hit.filename for hit in result.hits]
        rank = files.index(expected) + 1 if expected in files else None
        rows.append(
            {
                "case_id": case_id,
                "question": question,
                "expected": expected,
                "got": files,
                "rank": rank,
                "evidence": result.evidence,
                "elapsed_ms": result.elapsed_ms,
            }
        )
    return rows


def metrics(rows: list[dict], top_k: int) -> dict:
    n = len(rows) or 1
    return {
        "cases": len(rows),
        "top_k": top_k,
        "hit_at_1": round(sum(1 for r in rows if r["rank"] == 1) / n, 4),
        "hit_at_k": round(sum(1 for r in rows if r["rank"]) / n, 4),
        "mrr": round(sum(1 / r["rank"] for r in rows if r["rank"]) / n, 4),
        "evidence_rate": round(sum(1 for r in rows if r["evidence"]) / n, 4),
        "avg_ms": int(sum(r["elapsed_ms"] for r in rows) / n),
    }


@router.get("/api/kbs/{kb_id}/eval")
def get_eval(kb_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    kb = get_kb(db, kb_id, user)
    cases = db.scalars(select(EvalCase).where(EvalCase.kb_id == kb.id).order_by(EvalCase.id)).all()
    runs = db.scalars(select(EvalRun).where(EvalRun.kb_id == kb.id).order_by(EvalRun.id.desc()).limit(10)).all()
    return {
        "cases": [case_public(c) for c in cases],
        "runs": [run_public(r, details=False) for r in runs],
        "latest": run_public(runs[0], details=True) if runs else None,
    }


@router.post("/api/kbs/{kb_id}/eval/cases")
def add_case(kb_id: int, body: CaseBody, user: User = Depends(current_user), db: Session = Depends(get_db)):
    kb = get_kb(db, kb_id, user, write=True)
    case = EvalCase(kb_id=kb.id, question=body.question.strip(), expected=body.expected.strip())
    db.add(case)
    db.commit()
    return case_public(case)


@router.delete("/api/eval/cases/{case_id}")
def delete_case(case_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    case = db.get(EvalCase, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="评测问题不存在")
    get_kb(db, case.kb_id, user, write=True)
    db.delete(case)
    db.commit()
    return {"ok": True}


@router.post("/api/kbs/{kb_id}/eval/run")
async def run_eval(kb_id: int, body: RunBody, user: User = Depends(current_user), db: Session = Depends(get_db)):
    kb = get_kb(db, kb_id, user)
    cases = [(c.id, c.question, c.expected) for c in db.scalars(select(EvalCase).where(EvalCase.kb_id == kb.id).order_by(EvalCase.id))]
    if not cases:
        raise HTTPException(status_code=400, detail="先添加评测问题")
    rows = await asyncio.to_thread(evaluate, kb.id, cases, body.top_k)
    run = EvalRun(kb_id=kb.id, top_k=body.top_k, metrics=metrics(rows, body.top_k), details=rows, created_by=user.id)
    db.add(run)
    db.commit()
    return run_public(run, details=True)


@router.post("/api/messages/{message_id}/to-eval")
def message_to_eval(message_id: int, body: FromMessageBody, user: User = Depends(current_user), db: Session = Depends(get_db)):
    msg = db.get(Message, message_id)
    conv = db.get(Conversation, msg.conv_id) if msg else None
    if msg is None or conv is None or msg.role != "assistant" or (conv.user_id != user.id and user.role != "admin"):
        raise HTTPException(status_code=404, detail="消息不存在")
    kb = get_kb(db, body.kb_id, user, write=True)
    question = db.scalar(
        select(Message.content)
        .where(Message.conv_id == msg.conv_id, Message.role == "user", Message.id < msg.id)
        .order_by(Message.id.desc())
    )
    if not question:
        raise HTTPException(status_code=400, detail="找不到这条回答对应的问题")
    case = EvalCase(kb_id=kb.id, question=question[:500], expected=body.expected.strip())
    db.add(case)
    db.commit()
    return case_public(case)
