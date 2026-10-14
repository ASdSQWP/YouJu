"""混合检索：向量召回 + BM25 → RRF 融合 → 可选重排 → 依据闸门。"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import httpx
import numpy as np
from sqlalchemy import select

from youju.db import SessionLocal
from youju.models import Chunk, Document, KnowledgeBase
from youju.providers.base import ProviderError
from youju.providers.registry import LOCAL_EMBED, embedder_for, reranker
from youju.providers.rerank import rerank as call_rerank
from youju.rag.index import CACHE, index_text
from youju.textproc import key_terms, tokens

log = logging.getLogger("youju.retrieve")

RRF_K = 60
RECALL = 30

# coverage：问题有效词的 IDF 加权覆盖率；dense：嵌入模型余弦相似度；rerank：重排相关度
STRICTNESS = {
    "loose": {"coverage": 0.2, "dense": 0.45, "rerank": 0.15},
    "standard": {"coverage": 0.34, "dense": 0.55, "rerank": 0.3},
    "strict": {"coverage": 0.5, "dense": 0.65, "rerank": 0.5},
}


@dataclass
class Hit:
    chunk_id: int
    kb_id: int
    kb_name: str
    doc_id: int
    filename: str
    section: str
    page: int | None
    text: str
    scores: dict = field(default_factory=dict)

    def public(self) -> dict:
        return {
            "chunk_id": self.chunk_id,
            "kb_id": self.kb_id,
            "kb_name": self.kb_name,
            "doc_id": self.doc_id,
            "filename": self.filename,
            "section": self.section,
            "page": self.page,
            "text": self.text,
            "scores": dict(self.scores),
        }


@dataclass
class Retrieval:
    query: str
    hits: list[Hit]
    evidence: bool
    reason: str
    elapsed_ms: int
    notes: list[str] = field(default_factory=list)
    best: dict = field(default_factory=dict)

    def public(self) -> dict:
        return {
            "query": self.query,
            "hits": [hit.public() for hit in self.hits],
            "evidence": self.evidence,
            "reason": self.reason,
            "elapsed_ms": self.elapsed_ms,
            "notes": self.notes,
            "best": self.best,
        }


def _gate(best: dict, strictness: str, reranked: bool) -> tuple[bool, str]:
    th = STRICTNESS.get(strictness, STRICTNESS["standard"])
    cov, dense, rr = best["coverage"], best["dense"], best.get("rerank")
    if reranked and rr is not None and rr >= th["rerank"]:
        return True, f"重排相关度 {rr:.2f}"
    if cov >= th["coverage"]:
        return True, f"关键词覆盖 {cov:.0%}"
    if dense >= th["dense"]:
        return True, f"语义相似度 {dense:.2f}"
    return False, f"相关度不足（覆盖 {cov:.0%}，阈值 {th['coverage']:.0%}）"


def retrieve(
    kb_ids: list[int],
    query: str,
    top_k: int = 5,
    strictness: str = "standard",
    use_rerank: bool = True,
) -> Retrieval:
    started = time.perf_counter()
    query = (query or "").strip()

    def done(hits, evidence, reason, notes=None, best=None) -> Retrieval:
        return Retrieval(query, hits, evidence, reason, int((time.perf_counter() - started) * 1000), notes or [], best or {})

    db = SessionLocal()
    try:
        kbs = db.scalars(select(KnowledgeBase).where(KnowledgeBase.id.in_(kb_ids or [-1]))).all()
        if not kbs:
            return done([], False, "没有可检索的知识库")
        if not query:
            return done([], False, "问题为空")
        q_tokens = tokens(query)
        q_terms = set(key_terms(query))
        notes: list[str] = []
        qvecs: dict[str, np.ndarray | None] = {}
        pool: list[dict] = []
        for kb in kbs:
            ref = kb.embedding_ref or LOCAL_EMBED
            if ref not in qvecs:
                try:
                    qvecs[ref] = embedder_for(db, ref).embed([query])[0]
                except (ProviderError, httpx.HTTPError) as exc:
                    qvecs[ref] = None
                    notes.append(f"「{kb.name}」向量检索不可用，已只用关键词检索：{exc}")
            index = CACHE.get(db, kb)
            ranks: dict[int, dict] = {}
            for rank, (pos, score) in enumerate(index.dense(qvecs[ref], RECALL)):
                slot = ranks.setdefault(pos, {"rrf": 0.0})
                slot["dense"] = score
                slot["rrf"] += 1 / (RRF_K + rank + 1)
            for rank, (pos, score) in enumerate(index.sparse(q_tokens, RECALL)):
                slot = ranks.setdefault(pos, {"rrf": 0.0})
                slot["bm25"] = score
                slot["rrf"] += 1 / (RRF_K + rank + 1)
            for pos, slot in ranks.items():
                entry = index.entries[pos]
                pool.append(
                    {
                        "entry": entry,
                        "kb": kb,
                        "local": ref == LOCAL_EMBED,
                        "scores": {
                            "dense": round(slot.get("dense", 0.0), 4),
                            "bm25": round(slot.get("bm25", 0.0), 3),
                            "rrf": slot["rrf"],
                            "coverage": round(index.coverage(q_terms, pos), 3),
                        },
                    }
                )

        pool.sort(key=lambda c: c["scores"]["rrf"] + 0.02 * c["scores"]["coverage"], reverse=True)
        candidates = pool[: max(top_k * 3, 12)]

        reranked = False
        rr_cfg = reranker(db) if use_rerank and candidates else None
        if rr_cfg:
            try:
                docs = [index_text(c["entry"].filename, c["entry"].section, c["entry"].text) for c in candidates]
                for idx, score in call_rerank(rr_cfg, query, docs, len(docs)):
                    if 0 <= idx < len(candidates):
                        candidates[idx]["scores"]["rerank"] = round(score, 4)
                candidates.sort(key=lambda c: c["scores"].get("rerank", -1.0), reverse=True)
                reranked = True
            except (ProviderError, httpx.HTTPError, KeyError, ValueError) as exc:
                notes.append(f"重排服务不可用，已使用融合排序：{exc}")

        th = STRICTNESS.get(strictness, STRICTNESS["standard"])
        kept = [
            c
            for c in candidates
            if c["scores"]["coverage"] > 0
            or (not c["local"] and c["scores"]["dense"] >= th["dense"] - 0.15)
            or (reranked and c["scores"].get("rerank", 0) >= th["rerank"] / 2)
        ]
        top = kept[:top_k]
        best = {
            "coverage": max((c["scores"]["coverage"] for c in top), default=0.0),
            "dense": max((c["scores"]["dense"] for c in top if not c["local"]), default=0.0),
            "rerank": max((c["scores"].get("rerank", 0.0) for c in top), default=0.0) if reranked else None,
        }
        evidence, reason = _gate(best, strictness, reranked) if top else (False, "没有相关片段")
        hits = []
        for c in top:
            e = c["entry"]
            scores = dict(c["scores"])
            scores["rrf"] = round(scores["rrf"], 4)
            hits.append(Hit(e.chunk_id, c["kb"].id, c["kb"].name, e.doc_id, e.filename, e.section, e.page, e.text, scores))
        return done(hits, evidence, reason, notes, best)
    finally:
        db.close()


def neighbors(chunk_id: int, span: int = 1) -> list[Hit]:
    db = SessionLocal()
    try:
        chunk = db.get(Chunk, chunk_id)
        if chunk is None:
            return []
        doc = db.get(Document, chunk.doc_id)
        kb = db.get(KnowledgeBase, chunk.kb_id)
        rows = db.scalars(
            select(Chunk)
            .where(Chunk.doc_id == chunk.doc_id, Chunk.ord.between(chunk.ord - span, chunk.ord + span), Chunk.id != chunk.id)
            .order_by(Chunk.ord)
        ).all()
        return [
            Hit(r.id, r.kb_id, kb.name if kb else "", r.doc_id, doc.filename if doc else "", r.section, r.page, r.text, {})
            for r in rows
        ]
    finally:
        db.close()
