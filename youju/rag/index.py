"""每个知识库一份内存索引：向量矩阵 + BM25 倒排。知识库有改动时按版本号重建。"""

from __future__ import annotations

import math
import threading
from collections import Counter, defaultdict
from dataclasses import dataclass

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from youju.models import Chunk, Document, KnowledgeBase
from youju.textproc import tokens

K1, B = 1.5, 0.75


@dataclass
class Entry:
    chunk_id: int
    doc_id: int
    filename: str
    section: str
    page: int | None
    ord: int
    text: str


def index_text(filename: str, section: str, text: str) -> str:
    stem = filename.rsplit(".", 1)[0]
    return f"{stem} {section}\n{text}"


class KBIndex:
    def __init__(self, kb_id: int, version: int, entries: list[Entry], vectors: np.ndarray | None):
        self.kb_id = kb_id
        self.version = version
        self.entries = entries
        self.matrix = vectors
        self.n = len(entries)
        self.postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        self.lengths: list[int] = []
        self.token_sets: list[frozenset[str]] = []
        for pos, entry in enumerate(entries):
            toks = tokens(index_text(entry.filename, entry.section, entry.text))
            counts = Counter(toks)
            for term, freq in counts.items():
                self.postings[term].append((pos, freq))
            self.lengths.append(len(toks))
            self.token_sets.append(frozenset(counts))
        self.avgdl = (sum(self.lengths) / self.n) if self.n else 1.0
        self.max_idf = math.log(1 + (self.n + 0.5) / 0.5)
        self.by_chunk = {entry.chunk_id: pos for pos, entry in enumerate(entries)}

    def idf(self, term: str) -> float:
        df = len(self.postings.get(term, ()))
        return math.log(1 + (self.n - df + 0.5) / (df + 0.5))

    def sparse(self, query_tokens: list[str], k: int) -> list[tuple[int, float]]:
        scores: dict[int, float] = defaultdict(float)
        for term in set(query_tokens):
            postings = self.postings.get(term)
            if not postings:
                continue
            idf = self.idf(term)
            for pos, freq in postings:
                dl = self.lengths[pos] or 1
                scores[pos] += idf * freq * (K1 + 1) / (freq + K1 * (1 - B + B * dl / self.avgdl))
        return sorted(scores.items(), key=lambda item: item[1], reverse=True)[:k]

    def dense(self, qvec: np.ndarray | None, k: int) -> list[tuple[int, float]]:
        if qvec is None or self.matrix is None or not self.n or self.matrix.shape[1] != qvec.shape[0]:
            return []
        sims = self.matrix @ qvec
        k = min(k, self.n)
        top = np.argpartition(-sims, k - 1)[:k]
        top = top[np.argsort(-sims[top])]
        return [(int(i), float(sims[i])) for i in top]

    def coverage(self, query_terms: set[str], pos: int) -> float:
        """问题里的有效词按 IDF 加权后，有多少出现在这个切片里。

        知识库里从没出现过的词只算一半权重：它们多数是跨词碎片，
        但像「天气」这种真正的主题词仍然会把覆盖率拉低。
        """
        if not query_terms:
            return 0.0
        total = hit = 0.0
        for term in query_terms:
            weight = self.idf(term) if term in self.postings else 0.5 * self.max_idf
            total += weight
            if term in self.token_sets[pos]:
                hit += weight
        return hit / total if total > 0 else 0.0


def load_index(db: Session, kb: KnowledgeBase) -> KBIndex:
    rows = db.execute(
        select(
            Chunk.id, Chunk.doc_id, Document.filename, Chunk.section, Chunk.page, Chunk.ord, Chunk.text, Chunk.embedding
        )
        .join(Document, Document.id == Chunk.doc_id)
        .where(Chunk.kb_id == kb.id, Document.status == "ready")
        .order_by(Chunk.doc_id, Chunk.ord)
    ).all()
    entries = [Entry(r.id, r.doc_id, r.filename, r.section or "", r.page, r.ord, r.text) for r in rows]
    vectors = None
    blobs = [r.embedding for r in rows]
    if blobs and all(blobs):
        mats = [np.frombuffer(blob, dtype=np.float32) for blob in blobs]
        if len({m.shape[0] for m in mats}) == 1:
            vectors = np.vstack(mats)
    return KBIndex(kb.id, kb.index_version, entries, vectors)


class IndexCache:
    def __init__(self) -> None:
        self._items: dict[int, KBIndex] = {}
        self._lock = threading.Lock()

    def get(self, db: Session, kb: KnowledgeBase) -> KBIndex:
        with self._lock:
            cached = self._items.get(kb.id)
            if cached is not None and cached.version == kb.index_version:
                return cached
        built = load_index(db, kb)
        with self._lock:
            self._items[kb.id] = built
        return built

    def drop(self, kb_id: int) -> None:
        with self._lock:
            self._items.pop(kb_id, None)


CACHE = IndexCache()
