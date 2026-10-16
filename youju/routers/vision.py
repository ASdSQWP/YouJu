"""视觉检测：媒体库、点位/视频、算法规则、拉流监视和告警。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from youju.config import settings
from youju.db import get_db, utcnow
from youju.deps import current_user, get_agent, visible
from youju.models import Agent, DetectionAlert, DetectionRule, User, VisionFolder, VisionSource
from youju.providers.registry import LOCAL_CHAT, get_setting, list_models, resolve_chat, set_setting
from youju.vision.yolo import list_small_models
from youju.rag.ingest import safe_filename
from youju.serial import alert_public, rule_public, source_public
from youju.vision.catalog import ALGORITHMS, CATEGORIES, CATEGORY_BY_SCENARIO, DETECTORS
from youju.vision.compose import merge_model_json, parse_instruction
from youju.vision.engine import find_source_by_name, render_source, runtime, scenario_for_algorithms
from youju.vision.seed import seed_vision
from youju.vision.vlm import vision_ready

router = APIRouter(tags=["vision"])

MEDIA = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4", ".webm"}
IMAGE = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
VISIBILITIES = {"private", "team", "shared"}
DETECTOR_IDS = {d["id"] for d in DETECTORS}
WALL_RULE_PREFIX = "监视墙 · "


class SourceBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    code: str = Field(default="", max_length=64)
    kind: str = Field(default="url", max_length=16)
    uri: str = Field(default="", max_length=500)
    scenario: str = Field(default="gate", max_length=32)
    category: str = Field(default="other", max_length=32)
    visibility: str = Field(default="private", max_length=16)
    shared_user_ids: list[int] = Field(default_factory=list)


class SourcePatch(BaseModel):
    name: str | None = Field(default=None, max_length=80)
    category: str | None = Field(default=None, max_length=32)
    visibility: str | None = Field(default=None, max_length=16)
    shared_user_ids: list[int] | None = None
    scenario: str | None = Field(default=None, max_length=32)


class PullBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class SessionRuleBody(BaseModel):
    algorithms: list[str] = Field(default_factory=list)
    detector: str = Field(default="small", max_length=16)
    small_model: str = Field(default="demo", max_length=80)
    vlm_model: str = Field(default="", max_length=160)
    confidence: float = Field(default=0.55, ge=0.1, le=0.99)
    agent_id: int | None = None
    auto_push: bool = True


class BatchSourcesBody(BaseModel):
    ids: list[int] = Field(default_factory=list)


class MoveSourcesBody(BaseModel):
    ids: list[int] = Field(default_factory=list)
    folder_id: int | None = None


class FolderBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class VisionModelSettingsBody(BaseModel):
    small_model: str = Field(default="demo", max_length=80)
    yolo_path: str = Field(default="", max_length=500)
    vlm_model: str = Field(default="", max_length=160)


class VisionAssistBody(BaseModel):
    instruction: str = Field(min_length=1, max_length=1000)


class RuleBody(BaseModel):
    name: str = Field(default="", max_length=80)
    instruction: str = Field(default="", max_length=2000)
    source_id: int | None = None
    algorithms: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.55, ge=0.1, le=0.99)
    cooldown_sec: int = Field(default=20, ge=3, le=300)
    level: str = Field(default="medium", max_length=16)
    detector: str = Field(default="small", max_length=16)
    small_model: str = Field(default="demo", max_length=80)
    vlm_model: str = Field(default="", max_length=160)
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
    small_model: str | None = Field(default=None, max_length=80)
    vlm_model: str | None = Field(default=None, max_length=160)
    agent_id: int | None = None
    enabled: bool | None = None
    auto_push: bool | None = None
    auto_pull: bool | None = None


class ComposeBody(BaseModel):
    instruction: str = Field(min_length=1, max_length=2000)
    apply: bool = False
    use_model: bool = True


def _ensure_seed(db: Session, user: User) -> None:
    seed_vision(db, user)


def _normalize_visibility(value: str | None, shared: list[int] | None) -> tuple[str, list[int]]:
    vis = (value or "private").strip().lower()
    if vis not in VISIBILITIES:
        vis = "private"
    ids = sorted({int(x) for x in (shared or []) if int(x) > 0})
    if vis != "shared":
        ids = []
    return vis, ids


def _normalize_category(value: str | None, scenario: str = "") -> str:
    cat = (value or "").strip()[:32]
    if cat:
        return cat
    return CATEGORY_BY_SCENARIO.get(scenario or "", "other")


def _can_view_source(src: VisionSource, user: User) -> bool:
    if user.role == "admin" or src.owner_id == user.id:
        return True
    vis = getattr(src, "visibility", None) or "private"
    if vis == "team":
        return True
    if vis == "shared":
        return user.id in (getattr(src, "shared_user_ids", None) or [])
    return False


def _visible_sources(db: Session, user: User) -> list[VisionSource]:
    rows = db.scalars(select(VisionSource).order_by(VisionSource.id)).all()
    return [s for s in rows if _can_view_source(s, user)]


def _owners_map(db: Session, rows: list[VisionSource]) -> dict[int, User]:
    ids = {s.owner_id for s in rows}
    if not ids:
        return {}
    return {u.id: u for u in db.scalars(select(User).where(User.id.in_(ids))).all()}


def _source(db: Session, source_id: int, user: User, write: bool = False) -> VisionSource:
    src = db.get(VisionSource, source_id)
    if src is None or not _can_view_source(src, user):
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


def _source_payload(db: Session, src: VisionSource, user: User, owners: dict[int, User] | None = None) -> dict:
    owners = owners or _owners_map(db, [src])
    return source_public(
        src,
        runtime.boxes_of(src.id),
        runtime.tick_of(src.id),
        owner=owners.get(src.owner_id),
        viewer=user,
    )


def _sources_payload(db: Session, rows: list[VisionSource], user: User) -> list[dict]:
    owners = _owners_map(db, rows)
    return [_source_payload(db, src, user, owners) for src in rows]


def _rule_payload(db: Session, rule: DetectionRule) -> dict:
    source = db.get(VisionSource, rule.source_id) if rule.source_id else None
    agent = db.get(Agent, rule.agent_id) if rule.agent_id else None
    return rule_public(rule, source, agent)


def _chat_ready(db: Session) -> bool:
    ok, _ = vision_ready(db)
    if ok:
        return True
    for info in list_models(db, "chat"):
        if info.ref != LOCAL_CHAT:
            return True
    return False


def _parse_shared_ids(raw: str | None) -> list[int]:
    if not raw or not raw.strip():
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    out: list[int] = []
    for item in data:
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            continue
    return out


@router.get("/api/vision/catalog")
def catalog(user: User = Depends(current_user), db: Session = Depends(get_db)):
    _ensure_seed(db, user)
    vision_models = [
        {"ref": m.ref, "label": m.label, "vision": bool(m.vision), "model": m.model, "provider": m.provider}
        for m in list_models(db, "chat")
        if not m.local
    ]
    return {
        "algorithms": ALGORITHMS,
        "detectors": DETECTORS,
        "categories": CATEGORIES,
        "small_models": list_small_models(),
        "vision_models": vision_models,
        "defaults": {
            "small_model": get_setting(db, "vision_small_model") or "demo",
            "vlm_model": get_setting(db, "vision_vlm_model") or "",
            "yolo_path": get_setting(db, "vision_yolo_path") or "",
        },
    }


@router.get("/api/vision/vlm-status")
def vlm_status(user: User = Depends(current_user), db: Session = Depends(get_db)):
    ready, hint = vision_ready(db)
    return {
        "ready": ready,
        "hint": hint,
        "needs_media": True,
        "note": "大模型必须分析上传的图片/视频帧，告警会附带截图与画面依据；没有真实画面时不会凭空告警。",
    }


@router.get("/api/vision/sources")
def list_sources(group: str | None = None, user: User = Depends(current_user), db: Session = Depends(get_db)):
    _ensure_seed(db, user)
    rows = _visible_sources(db, user)
    payload = _sources_payload(db, rows, user)
    result: dict[str, Any] = {"sources": payload}
    if group == "owner":
        buckets: dict[int, dict] = {}
        for item in payload:
            oid = int(item["owner_id"])
            bucket = buckets.setdefault(oid, {"owner_id": oid, "owner_name": item["owner_name"] or f"用户{oid}", "sources": []})
            bucket["sources"].append(item)
        result["groups"] = list(buckets.values())
    folders = db.scalars(select(VisionFolder).where(VisionFolder.owner_id == user.id).order_by(VisionFolder.id)).all()
    result["folders"] = [{"id": f.id, "name": f.name, "owner_id": f.owner_id} for f in folders]
    return result


@router.post("/api/vision/sources")
def create_source(body: SourceBody, user: User = Depends(current_user), db: Session = Depends(get_db)):
    vis, shared = _normalize_visibility(body.visibility, body.shared_user_ids)
    scenario = body.scenario or "gate"
    src = VisionSource(
        owner_id=user.id,
        name=body.name.strip(),
        code=body.code.strip(),
        kind=body.kind if body.kind in ("demo", "file", "url") else "url",
        uri=body.uri.strip(),
        scenario=scenario,
        category=_normalize_category(body.category, scenario),
        visibility=vis,
        shared_user_ids=shared,
        status="idle",
    )
    db.add(src)
    db.commit()
    return _source_payload(db, src, user)


@router.post("/api/vision/sources/upload")
async def upload_sources(
    files: list[UploadFile] = File(...),
    category: str = Form("other"),
    visibility: str = Form("private"),
    shared_user_ids: str = Form("[]"),
    folder_id: str = Form(""),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    limit = settings.max_upload_mb * 1024 * 1024
    created: list[VisionSource] = []
    skipped: list[dict] = []
    folder = settings.upload_dir / "vision" / str(user.id)
    folder.mkdir(parents=True, exist_ok=True)
    vis, shared = _normalize_visibility(visibility, _parse_shared_ids(shared_user_ids))
    cat = _normalize_category(category)
    target_folder = None
    if str(folder_id).strip().isdigit():
        target_folder = db.get(VisionFolder, int(folder_id))
        if target_folder is None or (target_folder.owner_id != user.id and user.role != "admin"):
            raise HTTPException(status_code=404, detail="文件夹不存在")
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
        stem = Path(name).stem[:80]
        src = VisionSource(
            owner_id=user.id,
            name=stem,
            code=stem[:40],
            kind="file",
            uri=str(path),
            scenario="gate",
            category=cat,
            visibility=vis,
            shared_user_ids=shared,
            folder_id=target_folder.id if target_folder else None,
            status="idle",
            filename=name,
            mime=upload.content_type or ("video/mp4" if ext in {".mp4", ".webm"} else "image/jpeg"),
            extra={"preview": "video" if ext in {".mp4", ".webm"} else "image"},
        )
        db.add(src)
        db.flush()
        created.append(src)
    db.commit()
    return {"created": [_source_payload(db, s, user) for s in created], "skipped": skipped}


@router.patch("/api/vision/sources/{source_id}")
def patch_source(source_id: int, body: SourcePatch, user: User = Depends(current_user), db: Session = Depends(get_db)):
    src = _source(db, source_id, user, write=True)
    data = body.model_dump(exclude_unset=True)
    if "name" in data and data["name"]:
        src.name = data["name"].strip()[:80]
    if "scenario" in data and data["scenario"]:
        src.scenario = data["scenario"].strip()[:32]
    if "category" in data:
        src.category = _normalize_category(data["category"], src.scenario)
    if "visibility" in data or "shared_user_ids" in data:
        vis, shared = _normalize_visibility(
            data.get("visibility", src.visibility),
            data.get("shared_user_ids", src.shared_user_ids),
        )
        src.visibility = vis
        src.shared_user_ids = shared
    src.updated_at = utcnow()
    db.commit()
    return _source_payload(db, src, user)


@router.post("/api/vision/sources/{source_id}/session-rule")
def upsert_session_rule(
    source_id: int,
    body: SessionRuleBody,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """监视墙临时规则：绑定该源的启用规则，没有则创建一条。"""
    src = _source(db, source_id, user)
    algorithms = [a for a in body.algorithms if any(x["id"] == a for x in ALGORITHMS)]
    if not algorithms:
        raise HTTPException(status_code=400, detail="至少选择一个检测算法")
    detector = body.detector if body.detector in DETECTOR_IDS else "small"
    if body.agent_id:
        get_agent(db, body.agent_id, user)
    rule = db.scalar(
        select(DetectionRule)
        .where(
            DetectionRule.source_id == src.id,
            DetectionRule.enabled.is_(True),
            or_(DetectionRule.owner_id == user.id, DetectionRule.name.like(f"{WALL_RULE_PREFIX}%")),
        )
        .order_by(DetectionRule.id.desc())
    )
    if rule is None or (rule.owner_id != user.id and user.role != "admin"):
        # 优先找本人绑定该源的启用规则
        rule = db.scalar(
            select(DetectionRule)
            .where(DetectionRule.source_id == src.id, DetectionRule.owner_id == user.id, DetectionRule.enabled.is_(True))
            .order_by(DetectionRule.id.desc())
        )
    if rule is None:
        rule = DetectionRule(
            owner_id=user.id,
            name=f"{WALL_RULE_PREFIX}{src.name}"[:80],
            instruction="监视墙临时规则",
            source_id=src.id,
            algorithms=algorithms,
            confidence=body.confidence,
            cooldown_sec=20,
            level="medium",
            detector=detector,
            small_model=(body.small_model or "demo")[:80],
            vlm_model=(body.vlm_model or "")[:160],
            agent_id=body.agent_id,
            enabled=True,
            auto_push=body.auto_push,
            auto_pull=False,
        )
        db.add(rule)
    else:
        rule.algorithms = algorithms
        rule.detector = detector
        rule.small_model = (body.small_model or "demo")[:80]
        rule.vlm_model = (body.vlm_model or "")[:160]
        rule.confidence = body.confidence
        if body.agent_id is not None:
            rule.agent_id = body.agent_id
        rule.auto_push = body.auto_push
        rule.enabled = True
        rule.updated_at = utcnow()
    # 对齐小模型演示场景，避免上传图固定 gate 导致算法永不命中
    scene = scenario_for_algorithms(algorithms)
    src.scenario = scene
    if src.kind == "file" and (not src.category or src.category == "other"):
        src.category = CATEGORY_BY_SCENARIO.get(scene, src.category or "other")
    db.commit()
    return _rule_payload(db, rule)


@router.post("/api/vision/pull")
def pull_by_name(body: PullBody, user: User = Depends(current_user), db: Session = Depends(get_db)):
    _ensure_seed(db, user)
    src = find_source_by_name(db, body.name)
    if src is None or not _can_view_source(src, user):
        raise HTTPException(status_code=404, detail=f"没有找到名为「{body.name}」的视频")
    runtime.start_source(db, src)
    runtime.tick_live()
    db.refresh(src)
    return _source_payload(db, src, user)


@router.post("/api/vision/sources/{source_id}/start")
def start_source(source_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    src = _source(db, source_id, user)
    runtime.start_source(db, src)
    runtime.tick_live()
    db.refresh(src)
    return _source_payload(db, src, user)


@router.post("/api/vision/folders")
def create_folder(body: FolderBody, user: User = Depends(current_user), db: Session = Depends(get_db)):
    name = body.name.strip()
    row = VisionFolder(owner_id=user.id, name=name[:80])
    db.add(row)
    db.commit()
    return {"id": row.id, "name": row.name, "owner_id": row.owner_id}


@router.delete("/api/vision/folders/{folder_id}")
def delete_folder(folder_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    row = db.get(VisionFolder, folder_id)
    if row is None or (row.owner_id != user.id and user.role != "admin"):
        raise HTTPException(status_code=404, detail="文件夹不存在")
    for src in db.scalars(select(VisionSource).where(VisionSource.folder_id == row.id)).all():
        src.folder_id = None
    db.delete(row)
    db.commit()
    return {"ok": True}


@router.post("/api/vision/sources/move")
def move_sources(body: MoveSourcesBody, user: User = Depends(current_user), db: Session = Depends(get_db)):
    folder = None
    if body.folder_id is not None:
        folder = db.get(VisionFolder, body.folder_id)
        if folder is None or (folder.owner_id != user.id and user.role != "admin"):
            raise HTTPException(status_code=404, detail="文件夹不存在")
    moved = []
    for sid in body.ids:
        src = _source(db, sid, user, write=True)
        src.folder_id = folder.id if folder else None
        moved.append(src)
    db.commit()
    return {"sources": [_source_payload(db, s, user) for s in moved]}


@router.post("/api/vision/sources/batch-delete")
def batch_delete_sources(body: BatchSourcesBody, user: User = Depends(current_user), db: Session = Depends(get_db)):
    deleted = []
    for sid in body.ids:
        src = _source(db, sid, user, write=True)
        runtime.stop_source(db, src)
        db.delete(src)
        deleted.append(sid)
    db.commit()
    return {"deleted": deleted}


@router.get("/api/vision/model-settings")
def vision_model_settings(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return {
        "small_model": get_setting(db, "vision_small_model") or "demo",
        "yolo_path": get_setting(db, "vision_yolo_path") or "",
        "vlm_model": get_setting(db, "vision_vlm_model") or "",
        "small_models": list_small_models(),
        "vision_models": [
            {"ref": m.ref, "label": m.label, "vision": bool(m.vision)}
            for m in list_models(db, "chat")
            if not m.local
        ],
    }


@router.put("/api/vision/model-settings")
def save_vision_model_settings(body: VisionModelSettingsBody, user: User = Depends(current_user), db: Session = Depends(get_db)):
    set_setting(db, "vision_small_model", (body.small_model or "demo")[:80])
    set_setting(db, "vision_yolo_path", (body.yolo_path or "").strip()[:500])
    set_setting(db, "vision_vlm_model", (body.vlm_model or "").strip()[:160])
    db.commit()
    return vision_model_settings(user, db)


@router.post("/api/vision/model-settings/assist")
async def assist_vision_model_settings(body: VisionAssistBody, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """用已接入的大模型根据描述建议小模型与看图模型，不直接落库。"""
    text = body.instruction.strip()
    suggestion = {
        "small_model": "yolo" if any(k in text.lower() for k in ("yolo", "权重", ".pt")) else "demo",
        "vlm_model": get_setting(db, "vision_vlm_model") or get_setting(db, "default_chat_model") or "",
        "yolo_path": get_setting(db, "vision_yolo_path") or "",
        "note": "未调用大模型，已按关键词给出建议。",
        "model_used": False,
    }
    provider, info = resolve_chat(db)
    if provider is None:
        return suggestion
    smalls = ", ".join(m["id"] for m in list_small_models())
    visions = ", ".join(m.ref for m in list_models(db, "chat") if not m.local) or "（无）"
    prompt = (
        "根据用户对视觉检测的要求，输出 JSON："
        f"small_model(只能是这些 id 之一: {smalls}), "
        f"vlm_model(对话模型 ref，只能是这些或空字符串: {visions}), "
        "yolo_path(字符串，未知就留空), note(一句中文说明)。只输出 JSON。\n"
        f"用户要求：{text}"
    )
    raw = ""
    try:
        stream = provider.stream(info.model, [{"role": "user", "content": prompt}], temperature=0.1)
        async for part in stream:
            if part.kind == "text":
                raw += part.text
    except Exception:  # noqa: BLE001
        return suggestion
    import json
    import re

    match = re.search(r"\{[\s\S]*\}", raw)
    if not match:
        return suggestion
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return suggestion
    if isinstance(data, dict):
        if data.get("small_model"):
            suggestion["small_model"] = str(data["small_model"])[:80]
        if "vlm_model" in data:
            suggestion["vlm_model"] = str(data.get("vlm_model") or "")[:160]
        if data.get("yolo_path"):
            suggestion["yolo_path"] = str(data["yolo_path"])[:500]
        if data.get("note"):
            suggestion["note"] = str(data["note"])[:240]
        suggestion["model_used"] = True
    return suggestion


@router.post("/api/vision/sources/batch-start")
def batch_start_sources(body: BatchSourcesBody, user: User = Depends(current_user), db: Session = Depends(get_db)):
    started = []
    for sid in body.ids:
        src = _source(db, sid, user)
        runtime.start_source(db, src)
        started.append(src)
    if started:
        runtime.tick_live()
    return {"sources": [_source_payload(db, s, user) for s in started]}


@router.post("/api/vision/sources/{source_id}/stop")
def stop_source(source_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    src = _source(db, source_id, user)
    runtime.stop_source(db, src)
    return _source_payload(db, src, user)


@router.post("/api/vision/sources/batch-stop")
def batch_stop_sources(body: BatchSourcesBody, user: User = Depends(current_user), db: Session = Depends(get_db)):
    stopped = []
    for sid in body.ids:
        src = _source(db, sid, user)
        runtime.stop_source(db, src)
        stopped.append(src)
    return {"sources": [_source_payload(db, s, user) for s in stopped]}


@router.post("/api/vision/sources/{source_id}/tick")
def tick_source(source_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    src = _source(db, source_id, user)
    if src.status != "live":
        raise HTTPException(status_code=400, detail="这个点位还没有拉流")
    runtime.tick_live()
    db.refresh(src)
    return _source_payload(db, src, user)


@router.get("/api/vision/sources/{source_id}/preview")
def preview_source(source_id: int, advance: bool = False, user: User = Depends(current_user), db: Session = Depends(get_db)):
    src = _source(db, source_id, user)
    if advance and src.status == "live":
        runtime.tick_live()
        db.refresh(src)
    return _source_payload(db, src, user)


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
    detector = data.get("detector") or "small"
    if detector not in DETECTOR_IDS:
        detector = "small"
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
        detector=detector,
        small_model=(data.get("small_model") or "demo")[:80],
        vlm_model=(data.get("vlm_model") or "")[:160],
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


async def _enrich_draft_with_model(db: Session, draft: dict, instruction: str) -> dict:
    if not _chat_ready(db):
        draft["model_used"] = False
        draft["model_hint"] = "未配置对话模型，已用关键词解析。可选用大模型自动填表。"
        return draft
    provider, info = resolve_chat(db)
    if provider is None:
        draft["model_used"] = False
        draft["model_hint"] = "当前无可用对话模型，已用关键词解析。"
        return draft
    algo_ids = ", ".join(a["id"] for a in ALGORITHMS)
    det_ids = ", ".join(d["id"] for d in DETECTORS)
    prompt = (
        "把用户的检测要求整理成 JSON，字段："
        "name, algorithms(数组，只能用这些 id: "
        f"{algo_ids}), confidence(0.1-0.99), cooldown_sec, level(high|medium|low), "
        f"detector(只能是 {det_ids}), auto_pull(布尔), auto_push(布尔)。只输出 JSON。\n"
        f"用户要求：{instruction}"
    )
    text = ""
    try:
        stream = provider.stream(info.model, [{"role": "user", "content": prompt}], temperature=0.1)
        async for part in stream:
            if part.kind == "text":
                text += part.text
    except Exception:  # noqa: BLE001
        draft["model_used"] = False
        draft["model_hint"] = "大模型调用失败，已保留关键词解析结果。"
        return draft
    if text.strip():
        draft = merge_model_json(draft, text)
        draft["model_used"] = True
        draft["model_hint"] = "已用大模型辅助填写规则。"
    else:
        draft["model_used"] = False
        draft["model_hint"] = "大模型未返回内容，已用关键词解析。"
    return draft


@router.post("/api/vision/rules/compose")
async def compose_rule(body: ComposeBody, user: User = Depends(current_user), db: Session = Depends(get_db)):
    _ensure_seed(db, user)
    sources = [
        {"id": s.id, "name": s.name, "code": s.code}
        for s in _visible_sources(db, user)
    ]
    agents = [
        {"id": a.id, "name": a.name, "template": a.template}
        for a in db.scalars(select(Agent).where(visible(Agent, user))).all()
    ]
    try:
        draft = parse_instruction(body.instruction, sources, agents)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if body.use_model:
        draft = await _enrich_draft_with_model(db, draft, body.instruction)
    else:
        draft["model_used"] = False
        draft["model_hint"] = "已关闭大模型辅助。"
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
    if "detector" in data and data["detector"] and data["detector"] not in DETECTOR_IDS:
        raise HTTPException(status_code=400, detail="不支持的检测模式")
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
    sources = {s.id: s for s in _visible_sources(db, user)}
    if not sources:
        return {"alerts": []}
    query = select(DetectionAlert).where(DetectionAlert.source_id.in_(list(sources))).order_by(DetectionAlert.id.desc()).limit(80)
    if source_id:
        query = query.where(DetectionAlert.source_id == source_id)
    rules = {r.id: r for r in db.scalars(select(DetectionRule)).all()}
    return {"alerts": [alert_public(a, sources.get(a.source_id), rules.get(a.rule_id)) for a in db.scalars(query).all()]}


@router.get("/api/vision/alerts/{alert_id}/snapshot")
def alert_snapshot(alert_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    alert = db.get(DetectionAlert, alert_id)
    source = db.get(VisionSource, alert.source_id) if alert else None
    if alert is None or source is None or not _can_view_source(source, user):
        raise HTTPException(status_code=404, detail="告警不存在")
    path = Path(alert.snapshot_path or "")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="告警没有对应截图")
    return Response(path.read_bytes(), media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.post("/api/vision/alerts/{alert_id}/close")
def close_alert(alert_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    alert = db.get(DetectionAlert, alert_id)
    source = db.get(VisionSource, alert.source_id) if alert else None
    if alert is None or source is None or not _can_view_source(source, user):
        raise HTTPException(status_code=404, detail="告警不存在")
    if user.role != "admin" and source.owner_id != user.id:
        raise HTTPException(status_code=403, detail="只有创建者或管理员可以关闭告警")
    alert.status = "closed"
    db.commit()
    rule = db.get(DetectionRule, alert.rule_id) if alert.rule_id else None
    return alert_public(alert, source, rule)
