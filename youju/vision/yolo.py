"""可选的 YOLO 小模型。权重放在 data/models/*.pt，或用 YOUJU_YOLO_MODEL 指定。"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from youju.config import settings
from youju.vision.catalog import ALGORITHMS, algo_level, algo_name

log = logging.getLogger("youju.vision.yolo")

SMALL_MODELS_BASE = [
    {
        "id": "demo",
        "name": "场景演示",
        "kind": "demo",
        "ready": True,
        "hint": "按点位场景回放检测框，不读取真实像素。",
    },
]


def _saved_weight_path() -> str:
    try:
        from youju.db import SessionLocal
        from youju.providers.registry import get_setting

        db = SessionLocal()
        try:
            return str(get_setting(db, "vision_yolo_path") or "")
        finally:
            db.close()
    except Exception:  # noqa: BLE001
        return ""


def weight_files(extra: str | None = None) -> list[Path]:
    found: list[Path] = []
    for raw in (extra, _saved_weight_path(), os.getenv("YOUJU_YOLO_MODEL") or ""):
        text = (raw or "").strip()
        if not text:
            continue
        path = Path(text)
        if path.is_file() and path not in found:
            found.append(path)
    folder = settings.data_dir / "models"
    if folder.is_dir():
        for path in sorted(folder.glob("*.pt")):
            if path not in found:
                found.append(path)
    return found


def ultralytics_ready() -> bool:
    try:
        import ultralytics  # noqa: F401
    except ImportError:
        return False
    return True


def list_small_models(extra_weight: str | None = None) -> list[dict]:
    items = [dict(item) for item in SMALL_MODELS_BASE]
    weights = weight_files(extra_weight)
    ready = ultralytics_ready()
    if not weights:
        items.append(
            {
                "id": "yolo",
                "name": "YOLO",
                "kind": "yolo",
                "ready": False,
                "hint": "未找到权重。把 .pt 放到 data/models/，或设置环境变量 YOUJU_YOLO_MODEL。类名需与算法 id 一致（如 no_helmet）。",
            }
        )
        return items
    if not ready:
        hint_extra = "已找到权重，但未安装 ultralytics（pip install ultralytics）。"
    else:
        hint_extra = "使用本地 YOLO 权重检测真实画面。类名与算法 id 或中文名对齐才会命中。"
    for path in weights:
        items.append(
            {
                "id": f"yolo:{path.name}",
                "name": f"YOLO · {path.stem}",
                "kind": "yolo",
                "ready": ready,
                "path": str(path),
                "hint": hint_extra,
            }
        )
    return items


def resolve_weight(model_id: str, extra: str | None = None) -> Path | None:
    needle = (model_id or "").strip()
    weights = weight_files(extra)
    if not weights:
        return None
    if needle in ("", "yolo"):
        return weights[0]
    name = needle.split(":", 1)[1] if needle.startswith("yolo:") else needle
    for path in weights:
        if path.name == name or path.stem == name or str(path) == name:
            return path
    return None


def _algo_lookup() -> dict[str, str]:
    table: dict[str, str] = {}
    for item in ALGORITHMS:
        table[item["id"].lower()] = item["id"]
        table[item["name"].lower()] = item["id"]
    return table


def detect_yolo(
    jpeg: bytes | None,
    algorithms: list[str],
    confidence_min: float,
    rule_id: int,
    model_id: str = "yolo",
) -> tuple[list[dict], str]:
    """返回 (hits, hint)。没有权重或未安装时不编造结果。"""
    if not jpeg:
        return [], "没有可分析的真实画面，YOLO 无法检测"
    if not ultralytics_ready():
        return [], "未安装 ultralytics，无法运行 YOLO。请执行 pip install ultralytics"
    weight = resolve_weight(model_id)
    if weight is None:
        return [], "未找到 YOLO 权重。把 .pt 放到 data/models/，或设置 YOUJU_YOLO_MODEL"
    try:
        from ultralytics import YOLO
    except ImportError:
        return [], "未安装 ultralytics，无法运行 YOLO"
    try:
        model = YOLO(str(weight))
        results = model.predict(source=_as_image(jpeg), conf=max(0.05, float(confidence_min or 0.25)), verbose=False)
    except Exception as exc:  # noqa: BLE001
        log.exception("yolo predict failed")
        return [], f"YOLO 推理失败：{exc}"
    lookup = _algo_lookup()
    wanted = {a.lower(): a for a in algorithms} if algorithms else {}
    hits: list[dict] = []
    names = getattr(model, "names", {}) or {}
    for result in results or []:
        boxes = getattr(result, "boxes", None)
        if boxes is None:
            continue
        xyxyn = getattr(boxes, "xyxyn", None)
        confs = getattr(boxes, "conf", None)
        clss = getattr(boxes, "cls", None)
        if xyxyn is None or confs is None or clss is None:
            continue
        for i in range(len(clss)):
            cls_id = int(clss[i])
            label = str(names.get(cls_id, cls_id))
            algo_id = lookup.get(label.lower()) or wanted.get(label.lower())
            if algorithms and (algo_id is None or algo_id not in algorithms):
                continue
            if algo_id is None:
                continue
            conf = float(confs[i])
            if conf < float(confidence_min or 0):
                continue
            x1, y1, x2, y2 = [float(v) for v in xyxyn[i][:4]]
            hits.append(
                {
                    "id": algo_id,
                    "name": algo_name(algo_id),
                    "box": [x1, y1, max(0.0, x2 - x1), max(0.0, y2 - y1)],
                    "confidence": conf,
                    "level": algo_level(algo_id),
                    "detector": "yolo",
                    "rule_id": rule_id,
                    "from_image": False,
                    "demo": False,
                    "reason": f"YOLO（{weight.name}）检出 {label}",
                }
            )
    return hits, ""


def _as_image(jpeg: bytes):
    import io

    from PIL import Image

    return Image.open(io.BytesIO(jpeg)).convert("RGB")
