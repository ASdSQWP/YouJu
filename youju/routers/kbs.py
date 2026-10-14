"""知识库、文档上传和召回测试。"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from youju.config import settings
from youju.db import get_db, iso
from youju.deps import can_edit, current_user, get_kb, visible
from youju.models import Agent, Chunk, Document, EvalCase, EvalRun, KnowledgeBase, User
from youju.providers.registry import LOCAL_EMBED, find_model, get_setting
from youju.rag.index import CACHE
from youju.rag.ingest import ingest_document, reindex_kb, remove_document, safe_filename, save_upload
from youju.rag.parse import SUPPORTED
from youju.rag.retrieve import retrieve
from youju.serial import doc_public

router = APIRouter(tags=["knowledge"])


class KBBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)
    visibility: Literal["private", "team"] = "team"
    embedding_ref: str | None = Field(default=None, max_length=160)
    chunk_size: int = Field(default=500, ge=200, le=2000)
    chunk_overlap: int = Field(default=80, ge=0, le=400)


class KBPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=500)
    visibility: Literal["private", "team"] | None = None
    embedding_ref: str | None = Field(default=None, max_length=160)
    chunk_size: int | None = Field(default=None, ge=200, le=2000)
    chunk_overlap: int | None = Field(default=None, ge=0, le=400)


class SearchBody(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    top_k: int = Field(default=5, ge=1, le=20)
    strictness: Literal["loose", "standard", "strict"] = "standard"


def _stats(db: Session, kb_ids: list[int]) -> dict[int, dict]:
    stats = {kid: {"doc_count": 0, "chunk_count": 0, "ready_count": 0, "failed_count": 0, "processing_count": 0} for kid in kb_ids}
    if not kb_ids:
        return stats
    rows = db.execute(
        select(Document.kb_id, Document.status, func.count(Document.id), func.coalesce(func.sum(Document.chunk_count), 0))
        .where(Document.kb_id.in_(kb_ids))
        .group_by(Document.kb_id, Document.status)
    ).all()
    for kb_id, status, count, chunks in rows:
        item = stats[kb_id]
        item["doc_count"] += count
        if status == "ready":
            item["ready_count"] += count
            item["chunk_count"] += int(chunks)
        elif status == "failed":
            item["failed_count"] += count
        else:
            item["processing_count"] += count
    return stats


def _embedding_label(db: Session, ref: str) -> tuple[str, bool]:
    _, info = find_model(db, ref)
    return (info.label, True) if info else (f"{ref}（不可用）", False)


def kb_public(db: Session, kb: KnowledgeBase, user: User, stats: dict | None = None, owners: dict | None = None) -> dict:
    stats = stats if stats is not None else _stats(db, [kb.id])[kb.id]
    if owners is None:
        owner = db.get(User, kb.owner_id)
        owners = {kb.owner_id: owner.display_name if owner else ""}
    label, ok = _embedding_label(db, kb.embedding_ref)
    return {
        "id": kb.id,
        "name": kb.name,
        "description": kb.description,
        "visibility": kb.visibility,
        "owner_id": kb.owner_id,
        "owner_name": owners.get(kb.owner_id, ""),
        "embedding_ref": kb.embedding_ref,
        "embedding_label": label,
        "embedding_ok": ok,
        "embedding_dims": kb.embedding_dims,
        "chunk_size": kb.chunk_size,
        "chunk_overlap": kb.chunk_overlap,
        "can_edit": can_edit(user, kb),
        "created_at": iso(kb.created_at),
        "updated_at": iso(kb.updated_at),
        **stats,
    }


def _check_embedding(db: Session, ref: str) -> str:
    if ref == LOCAL_EMBED:
        return ref
    _, info = find_model(db, ref)
    if info is None or info.type != "embedding":
        raise HTTPException(status_code=400, detail=f"向量模型 {ref} 不可用")
    return ref


@router.get("/api/kbs")
def list_kbs(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(KnowledgeBase).where(visible(KnowledgeBase, user)).order_by(KnowledgeBase.id)).all()
    stats = _stats(db, [kb.id for kb in rows])
    owners = {u.id: u.display_name for u in db.scalars(select(User)).all()}
    return {"kbs": [kb_public(db, kb, user, stats[kb.id], owners) for kb in rows]}


@router.post("/api/kbs")
def create_kb(body: KBBody, user: User = Depends(current_user), db: Session = Depends(get_db)):
    ref = _check_embedding(db, body.embedding_ref or get_setting(db, "default_embedding_model") or LOCAL_EMBED)
    kb = KnowledgeBase(
        name=body.name.strip(),
        description=body.description.strip(),
        owner_id=user.id,
        visibility=body.visibility,
        embedding_ref=ref,
        chunk_size=body.chunk_size,
        chunk_overlap=min(body.chunk_overlap, body.chunk_size // 2),
    )
    db.add(kb)
    db.commit()
    return kb_public(db, kb, user)


@router.get("/api/kbs/{kb_id}")
def get_kb_detail(kb_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return kb_public(db, get_kb(db, kb_id, user), user)


@router.patch("/api/kbs/{kb_id}")
def update_kb(
    kb_id: int,
    body: KBPatch,
    background: BackgroundTasks,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    kb = get_kb(db, kb_id, user, write=True)
    rebuild = False
    if body.name is not None:
        kb.name = body.name.strip()
    if body.description is not None:
        kb.description = body.description.strip()
    if body.visibility is not None:
        kb.visibility = body.visibility
    if body.embedding_ref is not None and body.embedding_ref != kb.embedding_ref:
        kb.embedding_ref = _check_embedding(db, body.embedding_ref)
        rebuild = True
    if body.chunk_size is not None and body.chunk_size != kb.chunk_size:
        kb.chunk_size = body.chunk_size
        rebuild = True
    if body.chunk_overlap is not None and body.chunk_overlap != kb.chunk_overlap:
        kb.chunk_overlap = min(body.chunk_overlap, kb.chunk_size // 2)
        rebuild = True
    db.commit()
    if rebuild:
        background.add_task(reindex_kb, kb.id)
    return {**kb_public(db, kb, user), "reindexing": rebuild}


@router.delete("/api/kbs/{kb_id}")
def delete_kb(kb_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    kb = get_kb(db, kb_id, user, write=True)
    db.execute(delete(Chunk).where(Chunk.kb_id == kb.id))
    db.execute(delete(Document).where(Document.kb_id == kb.id))
    db.execute(delete(EvalCase).where(EvalCase.kb_id == kb.id))
    db.execute(delete(EvalRun).where(EvalRun.kb_id == kb.id))
    for agent in db.scalars(select(Agent)).all():
        if kb.id in (agent.kb_ids or []):
            agent.kb_ids = [k for k in agent.kb_ids if k != kb.id]
    db.delete(kb)
    db.commit()
    CACHE.drop(kb_id)
    shutil.rmtree(settings.upload_dir / str(kb_id), ignore_errors=True)
    return {"ok": True}


@router.get("/api/kbs/{kb_id}/documents")
def list_documents(kb_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    kb = get_kb(db, kb_id, user)
    docs = db.scalars(select(Document).where(Document.kb_id == kb.id).order_by(Document.id.desc())).all()
    return {"documents": [doc_public(d) for d in docs]}


@router.post("/api/kbs/{kb_id}/documents")
async def upload_documents(
    kb_id: int,
    background: BackgroundTasks,
    files: list[UploadFile] = File(...),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    kb = get_kb(db, kb_id, user, write=True)
    limit = settings.max_upload_mb * 1024 * 1024
    created: list[Document] = []
    skipped: list[dict] = []
    for upload in files:
        name = safe_filename(upload.filename or "")
        ext = Path(name).suffix.lower()
        if ext not in SUPPORTED:
            skipped.append({"filename": name, "reason": f"不支持 {ext or '无扩展名'} 格式"})
            continue
        data = await upload.read()
        if not data:
            skipped.append({"filename": name, "reason": "文件是空的"})
            continue
        if len(data) > limit:
            skipped.append({"filename": name, "reason": f"超过 {settings.max_upload_mb} MB"})
            continue
        sha = hashlib.sha256(data).hexdigest()
        if db.scalar(select(Document.id).where(Document.kb_id == kb.id, Document.sha256 == sha)):
            skipped.append({"filename": name, "reason": "知识库里已有内容相同的文件"})
            continue
        path = save_upload(kb.id, sha, ext, data)
        doc = Document(
            kb_id=kb.id,
            filename=name,
            ext=ext,
            size=len(data),
            sha256=sha,
            status="pending",
            storage_path=str(path),
            uploaded_by=user.id,
        )
        db.add(doc)
        db.flush()
        created.append(doc)
    db.commit()
    for doc in created:
        background.add_task(ingest_document, doc.id)
    return {"created": [doc_public(d) for d in created], "skipped": skipped}


def _doc(db: Session, doc_id: int, user: User, write: bool = False) -> Document:
    doc = db.get(Document, doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    get_kb(db, doc.kb_id, user, write=write)
    return doc


@router.get("/api/documents/{doc_id}/chunks")
def document_chunks(doc_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    doc = _doc(db, doc_id, user)
    rows = db.scalars(select(Chunk).where(Chunk.doc_id == doc.id).order_by(Chunk.ord)).all()
    return {
        "document": doc_public(doc),
        "chunks": [{"id": c.id, "ord": c.ord, "section": c.section, "page": c.page, "text": c.text} for c in rows],
    }


@router.get("/api/documents/{doc_id}/file")
def document_file(doc_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    doc = _doc(db, doc_id, user)
    path = Path(doc.storage_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="原文件已丢失")
    return FileResponse(path, filename=doc.filename)


@router.post("/api/documents/{doc_id}/retry")
def retry_document(
    doc_id: int, background: BackgroundTasks, user: User = Depends(current_user), db: Session = Depends(get_db)
):
    doc = _doc(db, doc_id, user, write=True)
    doc.status = "pending"
    doc.error = None
    db.commit()
    background.add_task(ingest_document, doc.id)
    return doc_public(doc)


@router.delete("/api/documents/{doc_id}")
def delete_document(doc_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    remove_document(db, _doc(db, doc_id, user, write=True))
    return {"ok": True}


@router.post("/api/kbs/{kb_id}/reindex")
def reindex(kb_id: int, background: BackgroundTasks, user: User = Depends(current_user), db: Session = Depends(get_db)):
    kb = get_kb(db, kb_id, user, write=True)
    background.add_task(reindex_kb, kb.id)
    return {"ok": True}


@router.post("/api/kbs/{kb_id}/search")
async def search(kb_id: int, body: SearchBody, user: User = Depends(current_user), db: Session = Depends(get_db)):
    kb = get_kb(db, kb_id, user)
    result = await asyncio.to_thread(retrieve, [kb.id], body.query, body.top_k, body.strictness)
    return result.public()
