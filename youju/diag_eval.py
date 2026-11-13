"""仿真规程上的小型检索评测。

只统计带了对应标签的题目，并给出分母。词面覆盖率不是事实核验。
默认跑哈希向量 + BM25，不调用付费模型。
"""

from __future__ import annotations

import json
from pathlib import Path

from youju.agent.citations import EMPTY_CHECK, verify
from youju.agent.offline import extractive_answer
from youju.agent.sources import SourceBook
from youju.config import SEED_DIR
from youju.rag.retrieve import retrieve

EVAL_FILE = SEED_DIR / "eval" / "diag.jsonl"
DOCS = SEED_DIR / "knowledge" / "diag"


def load_cases(path: Path | None = None) -> list[dict]:
    rows = []
    for line in (path or EVAL_FILE).read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _ratio(hit: int, denom: int) -> dict:
    return {"hit": hit, "denom": denom, "rate": round(hit / denom, 4) if denom else None}


def answer_case(kb_id: int, case: dict, strictness: str = "standard") -> dict:
    result = retrieve([kb_id], case["query"], top_k=3, strictness=strictness, use_rerank=False)
    top = result.hits[0] if result.hits else None
    files = [hit.filename for hit in result.hits]
    sections = [hit.section for hit in result.hits]
    concluded = bool(result.evidence)
    content = ""
    check = dict(EMPTY_CHECK)
    if concluded:
        book = SourceBook()
        for hit in result.hits:
            book.add(hit)
        content = extractive_answer(case["query"], book)
        check = verify(content, book, case["query"])
        if check.get("verdict") == "fail":
            concluded = False
    refused = not concluded
    file_ok = None
    if case.get("expect_file"):
        file_ok = bool(top and top.filename == case["expect_file"])
    fragment_ok = None
    if case.get("expect_section"):
        fragment_ok = any(case["expect_section"] in (section or "") for section in sections)
    evidence_ok = None
    if "expect_evidence" in case:
        evidence_ok = bool(result.evidence) is bool(case["expect_evidence"])
    refuse_ok = None
    if "expect_refuse" in case:
        refuse_ok = refused is bool(case["expect_refuse"])
    lexical_ok = None
    if case.get("expect_evidence") is True and concluded and check.get("total"):
        lexical_ok = check.get("verdict") in ("pass", "warn")
    constraint_ok = None
    if case.get("must_cite_contains") or case.get("must_not_conclude"):
        constraint_ok = True
        if case.get("must_cite_contains") and case["must_cite_contains"] not in content:
            constraint_ok = False
        if case.get("must_not_conclude") and case["must_not_conclude"] in content:
            constraint_ok = False
    reject_ok = None
    if case.get("reject_file"):
        reject_ok = not (top and top.filename == case["reject_file"])
    return {
        "id": case.get("id"),
        "kind": case.get("kind"),
        "query": case["query"],
        "files": files,
        "sections": sections,
        "top_file": top.filename if top else "",
        "evidence": bool(result.evidence),
        "gate_reason": result.reason,
        "refused": refused,
        "lexical_verdict": check.get("verdict"),
        "elapsed_ms": result.elapsed_ms,
        "file_ok": file_ok,
        "fragment_ok": fragment_ok,
        "evidence_ok": evidence_ok,
        "refuse_ok": refuse_ok,
        "lexical_ok": lexical_ok,
        "constraint_ok": constraint_ok,
        "reject_ok": reject_ok,
    }


def summarize(rows: list[dict]) -> dict:
    def count(key: str) -> dict:
        labeled = [row for row in rows if row[key] is not None]
        return _ratio(sum(1 for row in labeled if row[key]), len(labeled))

    return {
        "file_hit": count("file_ok"),
        "fragment_hit": count("fragment_ok"),
        "evidence_gate": count("evidence_ok"),
        "refuse": count("refuse_ok"),
        "citation_lexical": count("lexical_ok"),
        "constraint": count("constraint_ok"),
        "reject_top": count("reject_ok"),
        "note": "引用词面核验只比较用词重叠，不是事实核验。没有对应标签的题目不进入分母。",
    }


def run_labeled(kb_id: int, cases: list[dict] | None = None, strictness: str = "standard") -> dict:
    rows = [answer_case(kb_id, case, strictness) for case in (cases if cases is not None else load_cases())]
    return {
        "strictness": strictness,
        "embedding": "local:hash",
        "rerank": False,
        "latency_ms": sum(row["elapsed_ms"] for row in rows),
        "metrics": summarize(rows),
        "rows": rows,
    }
