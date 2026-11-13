"""把视觉文件读限制在授权上传目录内。

拒绝相对路径、`..`、以及目录或文件本身是符号链接的情况。
`realpath` 再核对一次，避免链接目标逃出根目录。合法上传是普通文件，预览走这条路径。
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from youju.config import settings


def vision_upload_root(owner_id: int) -> Path:
    return settings.upload_dir / "vision" / str(int(owner_id))


def alert_snapshot_root(source_id: int) -> Path:
    return settings.upload_dir / "vision" / "alerts" / str(int(source_id))


def contained_file(root: Path, raw: str | Path | None) -> Path | None:
    """返回根目录内的普通文件；任何越界或链接都返回 None。"""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text or "\x00" in text:
        return None
    try:
        root_real = Path(os.path.realpath(root))
        if not root_real.is_dir():
            return None
        lexical = Path(os.path.abspath(text))
        relative = lexical.relative_to(root_real)
    except (OSError, ValueError):
        return None
    if not relative.parts or any(part in ("", ".", "..") for part in relative.parts):
        return None
    current = root_real
    try:
        for part in relative.parts:
            current = current / part
            if os.path.islink(current) or not current.exists():
                return None
        if not current.is_file():
            return None
        real = Path(os.path.realpath(current))
        real.relative_to(root_real)
    except (OSError, ValueError):
        return None
    if not real.is_file():
        return None
    return real


def read_contained(root: Path, raw: str | Path | None) -> bytes | None:
    """打开已确认的普通文件。Linux 上不再跟随最后一级符号链接。"""
    path = contained_file(root, raw)
    if path is None:
        return None
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        probe = f"/proc/self/fd/{fd}"
        if os.path.exists(probe):
            fd_real = Path(os.path.realpath(probe))
            fd_real.relative_to(Path(os.path.realpath(root)))
        chunks: list[bytes] = []
        while True:
            block = os.read(fd, 1024 * 1024)
            if not block:
                break
            chunks.append(block)
        return b"".join(chunks)
    except (OSError, ValueError):
        return None
    finally:
        os.close(fd)
