"""从上传的图片/视频抽出一帧 JPEG，供大模型看图分析。"""

from __future__ import annotations

import io
import logging
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger("youju.vision.frames")

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
VIDEO_EXT = {".mp4", ".webm", ".avi", ".mkv", ".mov"}


def _to_jpeg(raw: bytes, max_side: int = 1280) -> bytes:
    from PIL import Image

    img = Image.open(io.BytesIO(raw))
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    elif img.mode == "L":
        img = img.convert("RGB")
    w, h = img.size
    scale = min(1.0, max_side / max(w, h))
    if scale < 1.0:
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=85, optimize=True)
    return out.getvalue()


def _ffmpeg_frame(path: Path, second: float = 0.0) -> bytes | None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{max(0.0, second):.2f}",
        "-i",
        str(path),
        "-frames:v",
        "1",
        "-f",
        "image2pipe",
        "-vcodec",
        "mjpeg",
        "-",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("ffmpeg extract failed: %s", exc)
        return None
    if proc.returncode != 0 or not proc.stdout:
        log.warning("ffmpeg extract empty: %s", (proc.stderr or b"")[:200])
        return None
    try:
        return _to_jpeg(proc.stdout)
    except Exception:  # noqa: BLE001
        return proc.stdout


def capture_jpeg(path: str | Path, tick: int = 0) -> bytes | None:
    """返回 JPEG 字节；失败返回 None。"""
    file = Path(path)
    if not file.is_file():
        return None
    ext = file.suffix.lower()
    try:
        if ext in IMAGE_EXT:
            return _to_jpeg(file.read_bytes())
        if ext in VIDEO_EXT:
            # 按 tick 在时间轴上取样，避免每秒看同一帧
            second = float(tick % 60)
            frame = _ffmpeg_frame(file, second=second)
            if frame:
                return frame
            # 无 ffmpeg 时无法可靠抽视频帧
            log.warning("无法从视频抽帧（需要本机安装 ffmpeg）: %s", file)
            return None
    except Exception:  # noqa: BLE001
        log.exception("capture_jpeg failed: %s", file)
        return None
    return None


def source_media_path(source) -> Path | None:
    if getattr(source, "kind", None) != "file":
        return None
    uri = getattr(source, "uri", None) or ""
    path = Path(uri)
    return path if path.is_file() else None
