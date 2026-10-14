"""文档入库：解析 → 切片 → 向量化 → 写库。在后台线程里跑。"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import httpx
import numpy as np
from sqlalchemy import delete, select, update

from youju.config import settings
from youju.db import SessionLocal, utcnow
from youju.models import Chunk, Document, KnowledgeBase
from youju.providers.base import ProviderError
from youju.providers.registry import embedder_for
from youju.rag.chunk import chunk_blocks
from youju.rag.index import CACHE, index_text
from youju.rag.parse import ParseError, parse_file

log = logging.getLogger("youju.ingest")


class IngestError(Exception):
    pass


def safe_filename(name: str) -> str:
    name = Path(name.replace("\\", "/")).name
    name = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "_", name).strip(" .")
    return (name or "未命名")[:200]


def save_upload(kb_id: int, sha256: str, ext: str, data: bytes) -> Path:
    folder = settings.upload_dir / str(kb_id)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{sha256[:24]}{ext}"
    path.write_bytes(data)
    return path


def _bump(db, kb_id: int) -> None:
    db.execute(
        update(KnowledgeBase)
        .where(KnowledgeBase.id == kb_id)
        .values(index_version=KnowledgeBase.index_version + 1, updated_at=utcnow())
    )


def ingest_document(doc_id: int) -> None:
    db = SessionLocal()
    try:
        doc = db.get(Document, doc_id)
        if doc is None:
            return
        kb = db.get(KnowledgeBase, doc.kb_id)
        if kb is None:
            return
        doc.status = "processing"
        doc.error = None
        db.commit()

        blocks = parse_file(Path(doc.storage_path), doc.filename)
        drafts = chunk_blocks(blocks, kb.chunk_size, kb.chunk_overlap)
        if not drafts:
            raise IngestError("没有解析出文字内容")
        embedder = embedder_for(db, kb.embedding_ref)
        vectors = embedder.embed([index_text(doc.filename, d.section, d.text) for d in drafts])
        dims = int(vectors.shape[1])

        db.refresh(kb)
        if kb.embedding_dims and kb.embedding_dims != dims:
            raise IngestError(f"向量维度 {dims} 与知识库已有的 {kb.embedding_dims} 不一致，请重建索引")
        kb.embedding_dims = dims
        db.execute(delete(Chunk).where(Chunk.doc_id == doc.id))
        for order, (draft, vector) in enumerate(zip(drafts, vectors)):
            db.add(
                Chunk(
                    kb_id=kb.id,
                    doc_id=doc.id,
                    ord=order,
                    section=draft.section[:255],
                    page=draft.page,
                    text=draft.text,
                    embedding=np.asarray(vector, dtype=np.float32).tobytes(),
                )
            )
        doc.status = "ready"
        doc.chunk_count = len(drafts)
        doc.char_count = sum(len(d.text) for d in drafts)
        doc.updated_at = utcnow()
        _bump(db, kb.id)
        db.commit()
    except (ParseError, IngestError, ProviderError, httpx.HTTPError, OSError, ValueError) as exc:
        db.rollback()
        _fail(db, doc_id, str(exc) or exc.__class__.__name__)
    except Exception as exc:  # noqa: BLE001
        log.exception("ingest failed")
        db.rollback()
        _fail(db, doc_id, f"处理失败：{exc.__class__.__name__}")
    finally:
        db.close()


def _fail(db, doc_id: int, message: str) -> None:
    doc = db.get(Document, doc_id)
    if doc is not None:
        doc.status = "failed"
        doc.error = message[:500]
        db.commit()


def remove_document(db, doc: Document) -> None:
    kb_id = doc.kb_id
    path = Path(doc.storage_path)
    db.execute(delete(Chunk).where(Chunk.doc_id == doc.id))
    db.delete(doc)
    _bump(db, kb_id)
    db.commit()
    still_used = db.scalar(select(Document.id).where(Document.storage_path == str(path)))
    if not still_used:
        path.unlink(missing_ok=True)


def reindex_kb(kb_id: int) -> None:
    db = SessionLocal()
    try:
        kb = db.get(KnowledgeBase, kb_id)
        if kb is None:
            return
        kb.embedding_dims = None
        doc_ids = list(db.scalars(select(Document.id).where(Document.kb_id == kb_id)).all())
        db.execute(update(Document).where(Document.kb_id == kb_id).values(status="pending", error=None))
        db.commit()
    finally:
        db.close()
    CACHE.drop(kb_id)
    for doc_id in doc_ids:
        ingest_document(doc_id)
