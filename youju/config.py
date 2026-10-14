from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
ROOT_DIR = PACKAGE_DIR.parent
WEB_DIR = PACKAGE_DIR / "web"
SEED_DIR = PACKAGE_DIR / "seed"


def _flag(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    database_url: str
    max_upload_mb: int
    session_days: int
    cookie_secure: bool
    admin_password: str | None

    @property
    def upload_dir(self) -> Path:
        return self.data_dir / "uploads"


def load_settings() -> Settings:
    data_dir = Path(os.getenv("YOUJU_DATA_DIR") or ROOT_DIR / "data").resolve()
    return Settings(
        data_dir=data_dir,
        database_url=os.getenv("YOUJU_DATABASE_URL") or f"sqlite:///{(data_dir / 'youju.db').as_posix()}",
        max_upload_mb=int(os.getenv("YOUJU_MAX_UPLOAD_MB", "20")),
        session_days=int(os.getenv("YOUJU_SESSION_DAYS", "7")),
        cookie_secure=_flag("YOUJU_COOKIE_SECURE", False),
        admin_password=os.getenv("YOUJU_ADMIN_PASSWORD") or None,
    )


settings = load_settings()
