from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from youju.config import settings


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.replace(microsecond=0).isoformat() + "Z"


def _make_engine(url: str):
    if not url.startswith("sqlite"):
        return create_engine(url, pool_pre_ping=True)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    return engine


engine = _make_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _sqlite_add_columns() -> None:
    """给已有 SQLite 库补列；create_all 不会改旧表。"""
    if not settings.database_url.startswith("sqlite"):
        return
    patches = {
        "detection_alerts": [
            ("snapshot_path", "VARCHAR(500) DEFAULT ''"),
            ("evidence", "JSON"),
        ],
        "messages": [
            ("attachments", "JSON"),
        ],
        "detection_rules": [
            ("small_model", "VARCHAR(80) DEFAULT 'demo'"),
            ("vlm_model", "VARCHAR(160) DEFAULT ''"),
        ],
        "vision_sources": [
            ("category", "VARCHAR(32) DEFAULT 'other'"),
            ("visibility", "VARCHAR(16) DEFAULT 'private'"),
            ("shared_user_ids", "JSON"),
            ("folder_id", "INTEGER"),
        ],
    }
    with engine.begin() as conn:
        for table, cols in patches.items():
            existing = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
            if not existing:
                continue
            for name, decl in cols:
                if name not in existing:
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def init_db() -> None:
    from youju import models  # noqa: F401

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(engine)
    _sqlite_add_columns()
