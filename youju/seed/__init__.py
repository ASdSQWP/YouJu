"""首次初始化时导入示例知识库、智能体和评测集。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from youju.config import SEED_DIR
from youju.models import Agent, Document, EvalCase, KnowledgeBase, User
from youju.providers.registry import LOCAL_EMBED
from youju.rag.ingest import ingest_document, save_upload
from youju.seed.templates import PACKS, TEMPLATES


def load_samples() -> list[dict]:
    return json.loads((SEED_DIR / "events.json").read_text(encoding="utf-8"))


def seed_demo(db: Session, owner: User) -> None:
    if db.scalar(select(func.count(KnowledgeBase.id))):
        return
    kb_ids: dict[str, int] = {}
    doc_ids: list[int] = []
    for pack in PACKS:
        kb = KnowledgeBase(
            name=pack["name"],
            description=pack["description"],
            owner_id=owner.id,
            visibility="team",
            embedding_ref=LOCAL_EMBED,
        )
        db.add(kb)
        db.flush()
        kb_ids[pack["key"]] = kb.id
        for path in sorted((SEED_DIR / "knowledge" / pack["key"]).glob("*.md")):
            data = path.read_bytes()
            sha = hashlib.sha256(data).hexdigest()
            stored = save_upload(kb.id, sha, path.suffix, data)
            doc = Document(
                kb_id=kb.id,
                filename=path.name,
                ext=path.suffix,
                size=len(data),
                sha256=sha,
                status="pending",
                storage_path=str(stored),
                uploaded_by=owner.id,
            )
            db.add(doc)
            db.flush()
            doc_ids.append(doc.id)
        eval_file = SEED_DIR / "eval" / f"{pack['key']}.jsonl"
        if eval_file.exists():
            for line in eval_file.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    row = json.loads(line)
                    db.add(EvalCase(kb_id=kb.id, question=row["question"], expected=row["expected"]))
    for tpl in TEMPLATES:
        if tpl.get("seed") is False:
            continue
        db.add(
            Agent(
                name=tpl["name"],
                emoji=tpl["emoji"],
                color=tpl["color"],
                category=tpl["category"],
                description=tpl["description"],
                system_prompt=tpl["system_prompt"],
                opening=tpl["opening"],
                suggestions=list(tpl["suggestions"]),
                kb_ids=[kb_ids[tpl["pack"]]] if tpl.get("pack") else [],
                strategy=tpl["strategy"],
                strict=tpl["strict"],
                allow_events=tpl["allow_events"],
                template=tpl["key"],
                owner_id=owner.id,
                visibility="team",
            )
        )
    db.commit()
    for doc_id in doc_ids:
        ingest_document(doc_id)


def seed_files() -> list[Path]:
    return sorted((SEED_DIR / "knowledge").rglob("*.md"))
