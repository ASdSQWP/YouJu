"""仿真排障记录、诊断、变更单和回归。审核只改这条记录的状态。"""

from __future__ import annotations

from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from youju.db import get_db, iso, utcnow
from youju.deps import current_user, get_kb
from youju.faults import CATEGORIES, IMPACT, diagnose_case, import_samples, redact_source, regression_summary
from youju.models import FaultCase, FaultChange, FaultDiagnosis, FaultRegression, User

router = APIRouter(tags=["faults"])


class CaseBody(BaseModel):
    title: str = Field(min_length=1, max_length=80)
    category: Literal["stream", "intercom", "version", "other"] = "other"
    phenomenon: str = Field(min_length=1, max_length=2000)
    version: str = Field(default="", max_length=40)
    log_text: str = Field(default="", max_length=8000)
    kb_ids: list[int] = Field(default_factory=list, max_length=8)


class DiagnoseBody(BaseModel):
    use_model: bool = False


class ChangePatch(BaseModel):
    suggestion: str | None = Field(default=None, max_length=4000)
    impact: str | None = Field(default=None, max_length=1000)
    recovery: str | None = Field(default=None, max_length=2000)
    checks: list[str] | None = Field(default=None, max_length=8)


class ReviewBody(BaseModel):
    action: Literal["approve", "reject"]
    note: str = Field(default="", max_length=500)


class RegressionBody(BaseModel):
    change_id: int
    item: str = Field(min_length=1, max_length=200)
    result: Literal["pass", "fail", "untested"]
    note: str = Field(default="", max_length=500)


def _case(db: Session, case_id: int, user: User) -> FaultCase:
    row = db.get(FaultCase, case_id)
    if row is None or (row.owner_id != user.id and user.role != "admin"):
        raise HTTPException(status_code=404, detail="排障记录不存在")
    return row


def _sources(db: Session, user: User, items: list | None) -> list[dict]:
    return [redact_source(db, user, item) for item in (items or []) if isinstance(item, dict)]


def _diag_public(db: Session, user: User, row: FaultDiagnosis) -> dict:
    inconclusive = row.status != "concluded"
    return {
        "id": row.id,
        "version": row.version,
        "status": row.status,
        "banner": "非结论" if inconclusive else "",
        "query": row.query,
        "observation": row.observation,
        "evidence": _sources(db, user, row.evidence),
        "candidates": _sources(db, user, row.candidates),
        "speculation": row.speculation,
        "content": row.content if not inconclusive else "",
        "check": row.verification if not inconclusive else {"verdict": "none", "total": 0},
        "gate_reason": row.gate_reason,
        "mode": row.mode,
        "model_ref": row.model_ref,
        "usage": row.usage or {},
        "latency_ms": row.latency_ms,
        "created_at": iso(row.created_at),
    }


def _change_public(db: Session, user: User, row: FaultChange, regs: list[FaultRegression]) -> dict:
    summary = regression_summary(row, regs)
    reviewer = db.get(User, row.reviewer_id) if row.reviewer_id else None
    return {
        "id": row.id,
        "diagnosis_id": row.diagnosis_id,
        "version": row.version,
        "problem": row.problem,
        "sources": _sources(db, user, row.sources),
        "suggestion": row.suggestion,
        "impact": row.impact or IMPACT,
        "recovery": row.recovery,
        "checks": list(row.checks or []),
        "status": row.status,
        "reviewer_id": row.reviewer_id,
        "reviewer_name": reviewer.display_name if reviewer else "",
        "reviewed_at": iso(row.reviewed_at),
        "review_note": row.review_note,
        "created_at": iso(row.created_at),
        "regression": summary,
    }


def _reg_public(row: FaultRegression) -> dict:
    return {
        "id": row.id,
        "change_id": row.change_id,
        "change_version": row.change_version,
        "item": row.item,
        "result": row.result,
        "note": row.note,
        "entry": row.entry,
        "created_at": iso(row.created_at),
    }


def _case_public(case: FaultCase) -> dict:
    return {
        "id": case.id,
        "title": case.title,
        "category": case.category,
        "category_label": CATEGORIES.get(case.category, case.category),
        "phenomenon": case.phenomenon,
        "version": case.version,
        "log_text": case.log_text,
        "kb_ids": list(case.kb_ids or []),
        "simulated": True,
        "entry": case.entry,
        "label": "仿真样例" if case.entry == "样例" else "人工录入 · 仿真",
        "created_at": iso(case.created_at),
        "updated_at": iso(case.updated_at),
    }


def _bundle(db: Session, user: User, case: FaultCase) -> dict:
    diags = db.scalars(select(FaultDiagnosis).where(FaultDiagnosis.case_id == case.id).order_by(FaultDiagnosis.version)).all()
    changes = db.scalars(select(FaultChange).where(FaultChange.case_id == case.id).order_by(FaultChange.version)).all()
    regs = db.scalars(select(FaultRegression).where(FaultRegression.case_id == case.id).order_by(FaultRegression.id)).all()
    reg_rows = list(regs)
    return {
        "case": _case_public(case),
        "diagnoses": [_diag_public(db, user, row) for row in diags],
        "changes": [_change_public(db, user, row, reg_rows) for row in changes],
        "regressions": [_reg_public(row) for row in reg_rows],
    }


def _markdown(bundle: dict) -> str:
    case = bundle["case"]
    lines = [
        f"# {case['title']}",
        "",
        "> 仿真数据。不是设备上报，不会下发到终端。回归结果若存在，也是人工录入，不是自动测试报告。",
        "",
        f"- 标记：{case['label']}",
        f"- 分类：{case['category_label']}",
        f"- 版本：{case['version'] or '未填'}",
        "",
        "## 系统观测",
        "",
        case["phenomenon"],
        "",
        "```",
        case["log_text"] or "（无日志）",
        "```",
        "",
    ]
    for diag in bundle["diagnoses"]:
        lines += [f"## 诊断 v{diag['version']}", ""]
        if diag["banner"]:
            lines += [f"**{diag['banner']}**。{diag['gate_reason']}", ""]
        lines += ["### 观测", "", diag["observation"], ""]
        if diag["evidence"]:
            lines += ["### 证据", ""]
            for src in diag["evidence"]:
                if src.get("redacted"):
                    lines.append(f"- [{src.get('n') or '-'}] {src['filename']}")
                else:
                    where = src["filename"] + (f" · {src['section']}" if src.get("section") else "")
                    lines += [f"- [{src.get('n') or '-'}] {where}（chunk {src.get('chunk_id')}）", "", src.get("text") or "", ""]
        if diag["candidates"]:
            lines += ["### 候选（非结论）", ""]
            for src in diag["candidates"]:
                if src.get("redacted"):
                    lines.append(f"- {src['filename']}")
                else:
                    lines.append(f"- {src['filename']} · {src.get('section') or ''}：{src.get('text') or ''}")
            lines.append("")
        if diag["speculation"]:
            lines += ["### 推测", "", diag["speculation"], ""]
    for change in bundle["changes"]:
        reg = change["regression"]
        lines += [
            f"## 变更单 v{change['version']} · {change['status']}",
            "",
            change["problem"],
            "",
            "### 建议",
            "",
            change["suggestion"],
            "",
            "### 影响范围",
            "",
            change["impact"],
            "",
            "### 恢复方案",
            "",
            change["recovery"],
            "",
            "### 验证项",
            "",
        ]
        lines += [f"- {item}" for item in change["checks"]]
        lines += [
            "",
            f"审核：{change['reviewer_name'] or '未审核'} {change['reviewed_at'] or ''} {change['review_note']}",
            f"回归：{reg['verdict']}（已记录 {reg['recorded']}/{reg['total']}）",
            "",
        ]
    if bundle["regressions"]:
        lines += ["## 回归记录（人工录入）", ""]
        for row in bundle["regressions"]:
            lines.append(f"- 变更 v{row['change_version']} · {row['item']} · {row['result']} · {row['note'] or '无备注'}")
        lines.append("")
    verdicts = [change["regression"]["verdict"] for change in bundle["changes"]]
    if "回归通过" not in verdicts:
        lines += ["## 结论", "", "未形成通过结论。未记录的验证项不能算通过。", ""]
    return "\n".join(lines)


@router.get("/api/faults")
def list_faults(user: User = Depends(current_user), db: Session = Depends(get_db)):
    query = select(FaultCase).order_by(FaultCase.id.desc())
    if user.role != "admin":
        query = query.where(FaultCase.owner_id == user.id)
    rows = db.scalars(query.limit(100)).all()
    return {"cases": [_case_public(row) for row in rows], "categories": CATEGORIES}


@router.post("/api/faults")
def create_fault(body: CaseBody, user: User = Depends(current_user), db: Session = Depends(get_db)):
    for kb_id in body.kb_ids:
        get_kb(db, kb_id, user)
    row = FaultCase(
        owner_id=user.id,
        title=body.title.strip(),
        category=body.category,
        phenomenon=body.phenomenon.strip(),
        version=body.version.strip(),
        log_text=body.log_text.strip(),
        kb_ids=list(dict.fromkeys(body.kb_ids)),
        simulated=True,
        entry="人工录入",
    )
    db.add(row)
    db.commit()
    return _case_public(row)


@router.post("/api/faults/samples")
def samples(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = import_samples(db, user)
    return {"cases": [_case_public(row) for row in rows]}


@router.get("/api/faults/{case_id}")
def get_fault(case_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return _bundle(db, user, _case(db, case_id, user))


@router.post("/api/faults/{case_id}/diagnose")
async def diagnose(case_id: int, body: DiagnoseBody, user: User = Depends(current_user), db: Session = Depends(get_db)):
    case = _case(db, case_id, user)
    diagnosis, change = await diagnose_case(db, user, case, use_model=body.use_model)
    bundle = _bundle(db, user, case)
    return {
        "diagnosis": _diag_public(db, user, diagnosis),
        "change": next(item for item in bundle["changes"] if item["id"] == change.id),
        **bundle,
    }


@router.patch("/api/faults/{case_id}/changes/{change_id}")
def edit_change(
    case_id: int,
    change_id: int,
    body: ChangePatch,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    case = _case(db, case_id, user)
    current = db.get(FaultChange, change_id)
    if current is None or current.case_id != case.id:
        raise HTTPException(status_code=404, detail="变更单不存在")
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="没有要修改的内容")
    if "checks" in fields:
        fields["checks"] = [item.strip()[:80] for item in fields["checks"] if item and item.strip()]
        if not fields["checks"]:
            raise HTTPException(status_code=400, detail="至少保留一个验证项")
    if current.status == "pending":
        for key, value in fields.items():
            setattr(current, key, value.strip() if isinstance(value, str) else value)
        db.commit()
        target = current
    else:
        # 已审核的版本保留，修改生成新的待审版本
        nxt = db.scalar(select(FaultChange.version).where(FaultChange.case_id == case.id).order_by(FaultChange.version.desc()))
        target = FaultChange(
            case_id=case.id,
            diagnosis_id=current.diagnosis_id,
            version=int(nxt or 0) + 1,
            problem=current.problem,
            sources=list(current.sources or []),
            suggestion=current.suggestion,
            impact=current.impact,
            recovery=current.recovery,
            checks=list(current.checks or []),
            status="pending",
            created_by=user.id,
        )
        for key, value in fields.items():
            setattr(target, key, value.strip() if isinstance(value, str) else value)
        db.add(target)
        db.commit()
        db.refresh(target)
    regs = list(db.scalars(select(FaultRegression).where(FaultRegression.case_id == case.id)).all())
    return _change_public(db, user, target, regs)


@router.post("/api/faults/{case_id}/changes/{change_id}/review")
def review_change(
    case_id: int,
    change_id: int,
    body: ReviewBody,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    case = _case(db, case_id, user)
    row = db.get(FaultChange, change_id)
    if row is None or row.case_id != case.id:
        raise HTTPException(status_code=404, detail="变更单不存在")
    if row.status != "pending":
        raise HTTPException(status_code=409, detail="只有待审变更单可以审核")
    row.status = "approved" if body.action == "approve" else "rejected"
    row.reviewer_id = user.id
    row.reviewed_at = utcnow()
    row.review_note = body.note.strip()
    db.commit()
    regs = list(db.scalars(select(FaultRegression).where(FaultRegression.case_id == case.id)).all())
    return _change_public(db, user, row, regs)


@router.post("/api/faults/{case_id}/regressions")
def add_regression(
    case_id: int,
    body: RegressionBody,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    case = _case(db, case_id, user)
    change = db.get(FaultChange, body.change_id)
    if change is None or change.case_id != case.id:
        raise HTTPException(status_code=404, detail="变更单不存在")
    if body.item not in (change.checks or []):
        raise HTTPException(status_code=400, detail="验证项不属于这张变更单")
    row = FaultRegression(
        case_id=case.id,
        change_id=change.id,
        change_version=change.version,
        item=body.item,
        result=body.result,
        note=body.note.strip(),
        entry="人工录入",
        created_by=user.id,
    )
    db.add(row)
    db.commit()
    regs = list(db.scalars(select(FaultRegression).where(FaultRegression.case_id == case.id)).all())
    return {"regression": _reg_public(row), "summary": regression_summary(change, regs)}


@router.get("/api/faults/{case_id}/export")
def export_fault(case_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    case = _case(db, case_id, user)
    text = _markdown(_bundle(db, user, case))
    filename = quote(f"{case.title}.md")
    return PlainTextResponse(
        text,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )
