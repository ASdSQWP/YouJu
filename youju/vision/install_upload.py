"""把对话里上传的权重和配置写进平台，而不是只提示用户去设置页。"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from youju.config import settings
from youju.deps import can_edit
from youju.models import Agent, ChatUpload, User
from youju.providers.registry import find_model, set_setting

WEIGHT_EXT = {".pt", ".onnx"}


def install_uploads(db: Session, user: User, agent: Agent | None, upload_ids: list[int]) -> list[str]:
    if not upload_ids:
        return []
    rows = db.scalars(select(ChatUpload).where(ChatUpload.user_id == user.id, ChatUpload.id.in_(upload_ids)).order_by(ChatUpload.id)).all()
    notes: list[str] = []
    folder = settings.data_dir / "models"
    for row in rows:
        src = Path(row.path or "")
        if not src.is_file():
            notes.append(f"「{row.name}」文件已经不在，未能写入")
            continue
        ext = src.suffix.lower()
        if ext in WEIGHT_EXT:
            folder.mkdir(parents=True, exist_ok=True)
            dest = folder / row.name
            if dest.resolve() != src.resolve():
                shutil.copyfile(src, dest)
            set_setting(db, "vision_yolo_path", str(dest))
            set_setting(db, "vision_small_model", f"yolo:{dest.name}")
            notes.append(f"已安装权重 {dest.name}，视觉小模型已改为 YOLO · {dest.stem}")
            continue
        if ext == ".json":
            notes.extend(_apply_json(db, user, agent, src, row.name))
    return notes


def _apply_json(db: Session, user: User, agent: Agent | None, path: Path, name: str) -> list[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [f"「{name}」不是合法的 JSON 配置，未写入"]
    if not isinstance(data, dict):
        return [f"「{name}」需要是 JSON 对象，未写入"]
    notes = []
    if data.get("small_model"):
        set_setting(db, "vision_small_model", str(data["small_model"])[:80])
        notes.append(f"视觉小模型已设为 {data['small_model']}")
    if "vlm_model" in data:
        set_setting(db, "vision_vlm_model", str(data.get("vlm_model") or "")[:160])
        notes.append("看图大模型已按配置文件更新")
    if data.get("yolo_path"):
        set_setting(db, "vision_yolo_path", str(data["yolo_path"]).strip()[:500])
        notes.append("YOLO 权重路径已按配置文件更新")
    ref = str(data.get("model_ref") or "").strip()
    if ref and agent is not None:
        if not can_edit(user, agent):
            notes.append("配置里的对话模型未写入：当前账号不能修改这个智能体")
        else:
            _row, info = find_model(db, ref)
            if info is None or info.type != "chat":
                notes.append(f"配置里的对话模型 {ref} 未接入，未修改智能体")
            else:
                agent.model_ref = ref
                notes.append(f"这个智能体的对话模型已改为 {info.label}")
    if not notes:
        notes.append(f"「{name}」里没有 small_model、vlm_model、yolo_path 或 model_ref，未改平台配置")
    return notes
