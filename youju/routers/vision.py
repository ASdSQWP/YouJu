"""视觉检测：点位/视频、算法规则、拉流监视和告警。"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from youju.config import settings
from youju.db import get_db, utcnow
from youju.deps import current_user, get_agent, visible
from youju.models import Agent, DetectionAlert, DetectionRule, User, VisionSource
from youju.rag.ingest import safe_filename
from youju.serial import alert_public, rule_public, source_public
from youju.vision.catalog import ALGORITHMS, DETECTORS
from youju.vision.compose import parse_instruction
from youju.vision.engine import find_source_by_name, render_source, runtime
from youju.vision.seed import seed_vision

router = APIRouter(tags=["vision"])

MEDIA = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4", ".webm"}
IMAGE = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


class SourceBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    code: str = Field(default="", max_length=64)
    kind: str = Field(default="url", max_length=16)
    uri: str = Field(default="", max_length=500)
    scenario: str = Field(default="gate", max_length=32)


class PullBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class RuleBody(BaseModel):
    name: str = Field(default="", max_length=80)
    instruction: str = Field(default="", max_length=2000)
    source_id: int | None = None
    algorithms: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.55, ge=0.1, le=0.99)
    cooldown_sec: int = Field(default=20, ge=3, le=300)
    level: str = Field(default="medium", max_length=16)
    detector: str = Field(default="small", max_length=16)
    agent_id: int | None = None
    enabled: bool = True
    auto_push: bool = True
    auto_pull: bool = False


class RulePatch(BaseModel):
    name: str | None = Field(default=None, max_length=80)
    instruction: str | None = None
    source_id: int | None = None
    algorithms: list[str] | None = None
    confidence: float | None = Field(default=None, ge=0.1, le=0.99)
    cooldown_sec: int | None = Field(default=None, ge=3, le=300)
    level: str | None = None
    detector: str | None = None
    agent_id: int | None = None
    enabled: bool | None = None
    auto_push: bool | None = None
    auto_pull: bool | None = None


class ComposeBody(BaseModel):
    instruction: str = Field(min_length=1, max_length=2000)
    apply: bool = False


def _ensure_seed(db: Session, user: User) -> None:
    seed_vision(db, user)


def _source(db: Session, source_id: int, user: User, write: bool = False) -> VisionSource:
    src = db.get(VisionSource, source_id)
    if src is None:
        raise HTTPException(status_code=404, detail="视频点位不存在")
    if write and user.role != "admin" and src.owner_id != user.id:
        raise HTTPException(status_code=403, detail="只有创建者或管理员可以修改")
    return src


def _rule(db: Session, rule_id: int, user: User, write: bool = False) -> DetectionRule:
    rule = db.get(DetectionRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="检测规则不存在")
    if write and user.role != "admin" and rule.owner_id != user.id:
        raise HTTPException(status_code=403, detail="只有创建者或管理员可以修改")
    return rule


def _sources_payload(db: Session, rows: list[VisionSource]) -> list[dict]:
    return [source_public(src, runtime.boxes_of(src.id), runtime.tick_of(src.id)) for src in rows]


def _rule_payload(db: Session, rule: DetectionRule) -> dict:
    source = db.get(VisionSource, rule.source_id) if rule.source_id else None
    agent = db.get(Agent, rule.agent_id) if rule.agent_id else None
    return rule_public(rule, source, agent)


@router.get("/api/vision/catalog")
def catalog(user: User = Depends(current_user), db: Session = Depends(get_db)):
    _ensure_seed(db, user)
    return {"algorithms": ALGORITHMS, "detectors": DETECTORS}


@router.get("/api/vision/sources")
def list_sources(user: User = Depends(current_user), db: Session = Depends(get_db)):
    _ensure_seed(db, user)
    rows = db.scalars(select(VisionSource).order_by(VisionSource.id)).all()
    return {"sources": _sources_payload(db, rows)}


@router.post("/api/vision/sources")
def create_source(body: SourceBody, user: User = Depends(current_user), db: Session = Depends(get_db)):
    src = VisionSource(
        owner_id=user.id,
        name=body.name.strip(),
        code=body.code.strip(),
        kind=body.kind if body.kind in ("demo", "file", "url") else "url",
        uri=body.uri.strip(),
        scenario=body.scenario or "gate",
        status="idle",
    )
    db.add(src)
    db.commit()
    return source_public(src)


@router.post("/api/vision/sources/upload")
async def upload_sources(
    files: list[UploadFile] = File(...),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    limit = settings.max_upload_mb * 1024 * 1024
    created: list[VisionSource] = []
    skipped: list[dict] = []
    folder = settings.upload_dir / "vision" / str(user.id)
    folder.mkdir(parents=True, exist_ok=True)
    for upload in files:
        name = safe_filename(upload.filename or "")
        ext = Path(name).suffix.lower()
        if ext not in MEDIA:
            skipped.append({"filename": name, "reason": f"不支持 {ext or '无扩展名'}，请上传图片或 mp4 / webm"})
            continue
        data = await upload.read()
        if not data:
            skipped.append({"filename": name, "reason": "文件是空的"})
            continue
        if len(data) > limit:
            skipped.append({"filename": name, "reason": f"超过 {settings.max_upload_mb} MB"})
            continue
        sha = hashlib.sha256(data).hexdigest()
        path = folder / f"{sha[:24]}{ext}"
        path.write_bytes(data)
        src = VisionSource(
            owner_id=user.id,
            name=Path(name).stem[:80],
            code="",
            kind="file",
            uri=str(path),
            scenario="gate",
            status="idle",
            filename=name,
            mime=upload.content_type or ("video/mp4" if ext in {".mp4", ".webm"} else "image/jpeg"),
            extra={"preview": "video" if ext in {".mp4", ".webm"} else "image"},
        )
        db.add(src)
        db.flush()
        created.append(src)
    db.commit()
    return {"created": [source_public(s) for s in created], "skipped": skipped}


@router.post("/api/vision/pull")
def pull_by_name(body: PullBody, user: User = Depends(current_user), db: Session = Depends(get_db)):
    _ensure_seed(db, user)
    src = find_source_by_name(db, body.name)
    if src is None:
        raise HTTPException(status_code=404, detail=f"没有找到名为「{body.name}」的视频")
    runtime.start_source(db, src)
    runtime.tick_live()
    db.refresh(src)
    return source_public(src, runtime.boxes_of(src.id), runtime.tick_of(src.id))


@router.post("/api/vision/sources/{source_id}/start")
def start_source(source_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    src = _source(db, source_id, user)
    runtime.start_source(db, src)
    runtime.tick_live()
    db.refresh(src)
    return source_public(src, runtime.boxes_of(src.id), runtime.tick_of(src.id))


@router.post("/api/vision/sources/{source_id}/stop")
def stop_source(source_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    src = _source(db, source_id, user)
    runtime.stop_source(db, src)
    return source_public(src)


@router.post("/api/vision/sources/{source_id}/tick")
def tick_source(source_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    src = _source(db, source_id, user)
    if src.status != "live":
        raise HTTPException(status_code=400, detail="这个点位还没有拉流")
    runtime.tick_live()
    db.refresh(src)
    return source_public(src, runtime.boxes_of(src.id), runtime.tick_of(src.id))


@router.get("/api/vision/sources/{source_id}/preview")
def preview_source(source_id: int, advance: bool = False, user: User = Depends(current_user), db: Session = Depends(get_db)):
    src = _source(db, source_id, user)
    if advance and src.status == "live":
        runtime.tick_live()
        db.refresh(src)
    return source_public(src, runtime.boxes_of(src.id), runtime.tick_of(src.id))


@router.get("/api/vision/sources/{source_id}/frame")
def source_frame(source_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    src = _source(db, source_id, user)
    png = render_source(src, runtime.tick_of(src.id), runtime.boxes_of(src.id))
    return Response(png, media_type="image/png", headers={"Cache-Control": "no-store"})


@router.get("/api/vision/sources/{source_id}/media")
def source_media(source_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    src = _source(db, source_id, user)
    if src.kind != "file" or not src.uri:
        raise HTTPException(status_code=404, detail="这个点位没有上传文件")
    path = Path(src.uri)
    if not path.exists():
        raise HTTPException(status_code=404, detail="文件已经不在")
    data = path.read_bytes()
    return Response(data, media_type=src.mime or "application/octet-stream", headers={"Cache-Control": "no-store"})


@router.delete("/api/vision/sources/{source_id}")
def delete_source(source_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    src = _source(db, source_id, user, write=True)
    runtime.stop_source(db, src)
    db.delete(src)
    db.commit()
    return {"ok": True}


@router.get("/api/vision/rules")
def list_rules(user: User = Depends(current_user), db: Session = Depends(get_db)):
    _ensure_seed(db, user)
    rows = db.scalars(select(DetectionRule).order_by(DetectionRule.id)).all()
    return {"rules": [_rule_payload(db, r) for r in rows]}


def _save_rule(db: Session, user: User, data: dict[str, Any]) -> DetectionRule:
    algorithms = [a for a in (data.get("algorithms") or []) if a]
    if not algorithms:
        raise HTTPException(status_code=400, detail="至少选择一个检测算法")
    agent_id = data.get("agent_id")
    if agent_id:
        get_agent(db, agent_id, user)
    source_id = data.get("source_id")
    if source_id:
        _source(db, source_id, user)
    name = (data.get("name") or "").strip() or "未命名规则"
    rule = DetectionRule(
        owner_id=user.id,
        name=name[:80],
        instruction=(data.get("instruction") or "").strip(),
        source_id=source_id,
        algorithms=algorithms,
        confidence=float(data.get("confidence") or 0.55),
        cooldown_sec=int(data.get("cooldown_sec") or 20),
        level=data.get("level") or "medium",
        detector=data.get("detector") or "small",
        agent_id=agent_id,
        enabled=bool(data.get("enabled", True)),
        auto_push=bool(data.get("auto_push", True)),
        auto_pull=bool(data.get("auto_pull", False)),
    )
    db.add(rule)
    db.commit()
    if rule.auto_pull and rule.source_id:
        src = db.get(VisionSource, rule.source_id)
        if src:
            runtime.start_source(db, src)
            runtime.tick_live()
    return rule


@router.post("/api/vision/rules/compose")
def compose_rule(body: ComposeBody, user: User = Depends(current_user), db: Session = Depends(get_db)):
    _ensure_seed(db, user)
    sources = [{"id": s.id, "name": s.name, "code": s.code} for s in db.scalars(select(VisionSource)).all()]
    agents = [
        {"id": a.id, "name": a.name, "template": a.template}
        for a in db.scalars(select(Agent).where(visible(Agent, user))).all()
    ]
    try:
        draft = parse_instruction(body.instruction, sources, agents)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not body.apply:
        return {"draft": draft, "applied": None}
    rule = _save_rule(db, user, draft)
    return {"draft": draft, "applied": _rule_payload(db, rule)}


@router.post("/api/vision/rules")
def create_rule(body: RuleBody, user: User = Depends(current_user), db: Session = Depends(get_db)):
    rule = _save_rule(db, user, body.model_dump())
    return _rule_payload(db, rule)


@router.patch("/api/vision/rules/{rule_id}")
def update_rule(rule_id: int, body: RulePatch, user: User = Depends(current_user), db: Session = Depends(get_db)):
    rule = _rule(db, rule_id, user, write=True)
    data = body.model_dump(exclude_unset=True)
    if "agent_id" in data and data["agent_id"]:
        get_agent(db, data["agent_id"], user)
    if "source_id" in data and data["source_id"]:
        _source(db, data["source_id"], user)
    if "algorithms" in data and data["algorithms"] is not None and not data["algorithms"]:
        raise HTTPException(status_code=400, detail="至少选择一个检测算法")
    for key, value in data.items():
        setattr(rule, key, value)
    rule.updated_at = utcnow()
    db.commit()
    return _rule_payload(db, rule)


@router.delete("/api/vision/rules/{rule_id}")
def delete_rule(rule_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    rule = _rule(db, rule_id, user, write=True)
    db.delete(rule)
    db.commit()
    return {"ok": True}


@router.get("/api/vision/alerts")
def list_alerts(source_id: int | None = None, user: User = Depends(current_user), db: Session = Depends(get_db)):
    sources = {s.id: s for s in db.scalars(select(VisionSource)).all()}
    if not sources:
        return {"alerts": []}
    query = select(DetectionAlert).where(DetectionAlert.source_id.in_(list(sources))).order_by(DetectionAlert.id.desc()).limit(80)
    if source_id:
        query = query.where(DetectionAlert.source_id == source_id)
    rules = {r.id: r for r in db.scalars(select(DetectionRule)).all()}
    return {"alerts": [alert_public(a, sources.get(a.source_id), rules.get(a.rule_id)) for a in db.scalars(query).all()]}


@router.post("/api/vision/alerts/{alert_id}/close")
def close_alert(alert_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    alert = db.get(DetectionAlert, alert_id)
    source = db.get(VisionSource, alert.source_id) if alert else None
    if alert is None or source is None or (user.role != "admin" and source.owner_id != user.id):
        raise HTTPException(status_code=404, detail="告警不存在")
    alert.status = "closed"
    db.commit()
    rule = db.get(DetectionRule, alert.rule_id) if alert.rule_id else None
    return alert_public(alert, source, rule)
