"""仿真排障闭环：记录、检索诊断、待审变更、人工回归、追溯。

不执行命令，不改文件，不连接设备。模型最多写出「推测」，不能变成证据。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from youju.access import readable_kbs
from youju.agent.citations import EMPTY_CHECK, verify
from youju.agent.offline import extractive_answer
from youju.agent.sources import SourceBook
from youju.config import SEED_DIR
from youju.db import utcnow
from youju.deps import can_read
from youju.models import (
    Document,
    FaultCase,
    FaultChange,
    FaultDiagnosis,
    FaultRegression,
    KnowledgeBase,
    User,
)
from youju.providers.base import StreamPart
from youju.providers.registry import LOCAL_EMBED, resolve_chat
from youju.rag.ingest import ingest_document, save_upload
from youju.rag.retrieve import retrieve

CATEGORIES = {
    "stream": "视频断流",
    "intercom": "对讲异常",
    "version": "版本不匹配",
    "other": "其他",
}
SAMPLE_KB = "仿真排障规程"
IMPACT = "只存在于这条仿真记录里。不会修改文件、不会执行命令，也不会下发到设备或生产环境。"
REDACTED = "来源已不可见"


def observation_text(case: FaultCase) -> str:
    kind = "仿真样例" if case.entry == "样例" else "人工录入的仿真文本"
    return (
        f"【系统观测】以下内容来自{kind}，不是设备上报。\n"
        f"分类：{CATEGORIES.get(case.category, case.category)}\n"
        f"版本：{case.version or '未填'}\n"
        f"现象：{case.phenomenon}\n"
        f"已脱敏日志：\n{case.log_text or '（空）'}"
    )


def _hit_public(hit, number: int | None = None) -> dict:
    data = hit.public()
    data["n"] = number
    data["redacted"] = False
    return data


def redact_source(db: Session, user: User, item: dict) -> dict:
    kb = db.get(KnowledgeBase, item.get("kb_id")) if item.get("kb_id") else None
    if kb is None or not can_read(user, kb):
        return {
            "n": item.get("n"),
            "chunk_id": item.get("chunk_id"),
            "kb_id": item.get("kb_id"),
            "doc_id": None,
            "filename": REDACTED,
            "section": "",
            "page": None,
            "text": "",
            "scores": {},
            "redacted": True,
        }
    return {**item, "redacted": False}


def _checks_from_hits(hits) -> list[str]:
    found = []
    for hit in hits:
        if "验证" not in (hit.section or ""):
            continue
        for line in hit.text.replace("。", "\n").splitlines():
            line = line.strip(" -\t")
            if line:
                found.append(line[:80])
    if not found:
        found = ["人工核对现象是否对应所引规程", "记录本次回归结果"]
    deduped = []
    for item in found:
        if item not in deduped:
            deduped.append(item)
    return deduped[:6]


def _recovery_from_hits(hits) -> str:
    lines = [hit.text.strip() for hit in hits if "恢复" in (hit.section or "")]
    if not lines:
        return "规程摘录里没有单独的恢复步骤。"
    return "\n".join(lines)[:800]


def _suggestion(concluded: bool, content: str, hits) -> str:
    if not concluded:
        return "依据不足，这不是结论。下面如果出现候选片段，只供人工核对，不能当作已经确认的原因或已经批准的变更。"
    quoted = content.strip() or "\n".join(f"[{hit.public()['filename']}] {hit.text[:180]}" for hit in hits)
    return "依据规程摘录的建议，尚未执行，也还没有审核：\n" + quoted[:1200]


async def diagnose_case(db: Session, user: User, case: FaultCase, use_model: bool = False) -> tuple[FaultDiagnosis, FaultChange]:
    allowed = readable_kbs(db, user, list(case.kb_ids or []))
    query = " ".join(
        part
        for part in (CATEGORIES.get(case.category, ""), case.phenomenon, case.version, case.log_text[:400])
        if part
    )[:500]
    result = retrieve([kb.id for kb in allowed], query, top_k=4, strictness="standard") if allowed else None
    gate_ok = bool(result and result.evidence)
    pool = list(result.hits) if result else []
    book = SourceBook()
    content = ""
    check = dict(EMPTY_CHECK)
    if gate_ok:
        for hit in pool:
            book.add(hit)
        content = extractive_answer(query, book)
        check = verify(content, book, query)
    concluded = gate_ok and check.get("verdict") != "fail"
    evidence_hits = pool if concluded else []
    candidate_hits = [] if concluded else pool
    if not concluded:
        content = ""
    speculation, mode, model_ref, usage = ("", "offline", "", {})
    if use_model:
        speculation, mode, model_ref, usage = await speculate_async(db, query, content, concluded)
    version = int(db.scalar(select(func.max(FaultDiagnosis.version)).where(FaultDiagnosis.case_id == case.id)) or 0) + 1
    diagnosis = FaultDiagnosis(
        case_id=case.id,
        version=version,
        status="concluded" if concluded else "inconclusive",
        query=query,
        observation=observation_text(case),
        evidence=[_hit_public(hit, book.add(hit)) for hit in evidence_hits],
        candidates=[_hit_public(hit) for hit in candidate_hits],
        speculation=speculation,
        content=content,
        verification=check if concluded else dict(EMPTY_CHECK),
        gate_reason=(result.reason if result else "没有可检索的知识库"),
        mode=mode,
        model_ref=model_ref,
        usage=usage,
        latency_ms=int(result.elapsed_ms) if result else 0,
        created_by=user.id,
    )
    db.add(diagnosis)
    db.flush()
    change = _new_change(db, user, case, diagnosis, evidence_hits, concluded, content)
    case.updated_at = utcnow()
    db.commit()
    db.refresh(diagnosis)
    db.refresh(change)
    return diagnosis, change


async def speculate_async(db: Session, query: str, evidence: str, concluded: bool) -> tuple[str, str, str, dict]:
    provider, info = resolve_chat(db)
    if provider is None:
        return "未接入对话模型，本次没有调用模型，也没有补充推测。", "offline", "", {}
    prompt = (
        "你只能写出未执行的推测，并且单独成段。不要声称已经修复或已经批准。"
        "证据不足时只写：依据不足。\n"
        f"问题：{query}\n证据摘录：{evidence or '无'}\n是否已过依据闸门：{'是' if concluded else '否'}"
    )
    text = ""
    usage: dict = {}
    try:
        async for part in provider.stream(info.model, [{"role": "user", "content": prompt}], temperature=0.1):
            if not isinstance(part, StreamPart):
                part = part
            if getattr(part, "kind", "") == "text":
                text += part.text
            elif getattr(part, "kind", "") == "usage" and part.usage:
                usage = dict(part.usage)
    except Exception as exc:  # noqa: BLE001
        return f"模型调用失败，未把推测写入结论：{exc.__class__.__name__}", "offline", "", {}
    text = text.strip()[:800]
    if not text:
        text = "模型没有返回推测。"
    return "【推测】" + text, "model", info.ref, usage


def _new_change(db, user, case, diagnosis, hits, concluded: bool, content: str) -> FaultChange:
    version = int(db.scalar(select(func.max(FaultChange.version)).where(FaultChange.case_id == case.id)) or 0) + 1
    change = FaultChange(
        case_id=case.id,
        diagnosis_id=diagnosis.id,
        version=version,
        problem=f"{case.title}（{CATEGORIES.get(case.category, case.category)} / {case.version or '未填版本'}）\n{case.phenomenon}",
        sources=diagnosis.evidence,
        suggestion=_suggestion(concluded, content, hits),
        impact=IMPACT,
        recovery=_recovery_from_hits(hits) if concluded else "没有足够依据，不生成恢复步骤。",
        checks=_checks_from_hits(hits) if concluded else ["不要把候选片段当成结论", "补充规程或改写现象后再诊断"],
        status="pending",
        created_by=user.id,
    )
    db.add(change)
    return change


def regression_summary(change: FaultChange, rows: list[FaultRegression]) -> dict:
    items = [str(item) for item in (change.checks or [])]
    latest: dict[str, FaultRegression] = {}
    for row in rows:
        if row.change_id == change.id and row.item in items:
            latest[row.item] = row
    if not items or len(latest) < len(items) or any(row.result != "pass" for row in latest.values()):
        missing = [item for item in items if item not in latest]
        failed = [item for item, row in latest.items() if row.result != "pass"]
        return {
            "verdict": "未形成通过结论",
            "passed": False,
            "recorded": len(latest),
            "total": len(items),
            "missing": missing,
            "not_pass": failed,
        }
    return {"verdict": "回归通过", "passed": True, "recorded": len(latest), "total": len(items), "missing": [], "not_pass": []}


def latest_change(db: Session, case_id: int) -> FaultChange | None:
    return db.scalar(select(FaultChange).where(FaultChange.case_id == case_id).order_by(FaultChange.version.desc()))


def import_samples(db: Session, user: User) -> list[FaultCase]:
    kb = db.scalar(
        select(KnowledgeBase).where(KnowledgeBase.owner_id == user.id, KnowledgeBase.name == SAMPLE_KB)
    )
    if kb is None:
        kb = KnowledgeBase(
            name=SAMPLE_KB,
            description="虚构的视频断流、对讲和版本规程，只用于仿真演示。",
            owner_id=user.id,
            visibility="private",
            embedding_ref=LOCAL_EMBED,
        )
        db.add(kb)
        db.flush()
        doc_ids = []
        for path in sorted((SEED_DIR / "knowledge" / "diag").glob("*.md")):
            doc_ids.append(_store_doc(db, kb, path, user.id))
        db.commit()
        for doc_id in doc_ids:
            ingest_document(doc_id)
    specs = [
        (
            "仿真：视频断流",
            "stream",
            "demo-player-1.4.2",
            "监视画面突然黑屏，拉流中断，连续数秒没有视频帧。",
            "2026-11-14T10:00:01 仿真日志 camera=demo-cam-03 event=断流 frames=0",
        ),
        (
            "仿真：对讲异常",
            "intercom",
            "demo-talk-2.0.1",
            "对讲没有声音，听得到对方，但自己说出去没有回传。",
            "2026-11-14T10:03:12 仿真日志 talk=demo-handset event=上行无声",
        ),
        (
            "仿真：版本不匹配",
            "version",
            "client-0.9 / server-1.3",
            "客户端版本和服务端版本不一致，接口提示版本不匹配。",
            "2026-11-14T10:06:40 仿真日志 handshake=版本不匹配 client=0.9 server=1.3",
        ),
        (
            "仿真：依据不足",
            "other",
            "n/a",
            "食堂周五供应什么主食。这条和视频、对讲、版本规程都无关。",
            "2026-11-14T10:08:00 仿真日志 topic=食堂 与拉流和对讲无关",
        ),
    ]
    created = []
    for title, category, version, phenomenon, log_text in specs:
        row = db.scalar(select(FaultCase).where(FaultCase.owner_id == user.id, FaultCase.title == title))
        if row is None:
            row = FaultCase(
                owner_id=user.id,
                title=title,
                category=category,
                phenomenon=phenomenon,
                version=version,
                log_text=log_text,
                kb_ids=[kb.id],
                simulated=True,
                entry="样例",
            )
            db.add(row)
            db.flush()
        created.append(row)
    db.commit()
    return created


def _store_doc(db: Session, kb: KnowledgeBase, path: Path, user_id: int) -> int:
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
        uploaded_by=user_id,
    )
    db.add(doc)
    db.flush()
    return doc.id
