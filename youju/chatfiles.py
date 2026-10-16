"""对话附件：按日期和文件类型分目录保存。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from youju.config import settings
from youju.rag.ingest import safe_filename

IMAGE = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
VIDEO = {".mp4", ".webm", ".mov"}
DOCUMENT = {".pdf", ".txt", ".md", ".doc", ".docx", ".csv", ".json", ".xlsx", ".xls", ".ppt", ".pptx"}
BLOCKED = {".exe", ".bat", ".cmd", ".ps1", ".sh", ".dll", ".js", ".msi"}


def classify_ext(name: str) -> tuple[str, str]:
    ext = Path(name).suffix.lower()
    if ext in BLOCKED:
        raise ValueError(f"不允许上传 {ext} 文件")
    if ext in IMAGE:
        return "images", ext
    if ext in VIDEO:
        return "videos", ext
    if ext in DOCUMENT:
        return "documents", ext
    if not ext:
        raise ValueError("文件需要有扩展名")
    return "other", ext


def store_upload(user_id: int, upload_id: int, filename: str, data: bytes) -> tuple[str, str, Path]:
    """返回 (kind, safe_name, path)。目录形如 uploads/chat/{用户}/{日期}/{类型}/。"""
    kind, ext = classify_ext(filename)
    day = datetime.now().strftime("%Y-%m-%d")
    folder = settings.upload_dir / "chat" / str(user_id) / day / kind
    folder.mkdir(parents=True, exist_ok=True)
    safe = safe_filename(filename) or f"file{ext}"
    path = folder / f"{upload_id}_{safe}"
    path.write_bytes(data)
    return kind, safe, path
