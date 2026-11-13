"""离线跑仿真规程评测。默认只有哈希向量 + BM25，不调用付费模型。

    python scripts/diag_baseline.py
    python scripts/diag_baseline.py --strictness strict

真实 embedding / rerank 需要你显式传入已经接好的模型引用。
没有配置时直接说明跳过，不编造时延或提升。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _prepare() -> tuple[int, str]:
    data = tempfile.mkdtemp(prefix="youju-diag-")
    os.environ["YOUJU_DATA_DIR"] = data
    os.environ["YOUJU_DATABASE_URL"] = f"sqlite:///{Path(data) / 'youju.db'}"
    os.environ["YOUJU_ADAPT_LOCAL"] = "0"
    from youju.db import SessionLocal, init_db
    from youju.faults import import_samples
    from youju.models import User
    from youju.security import hash_password

    init_db()
    db = SessionLocal()
    try:
        user = User(username="diag-baseline", display_name="评测", password_hash=hash_password("diag-pass-123"), role="admin")
        db.add(user)
        db.commit()
        import_samples(db, user)
        from sqlalchemy import select
        from youju.models import KnowledgeBase
        from youju.faults import SAMPLE_KB

        kb = db.scalar(select(KnowledgeBase).where(KnowledgeBase.name == SAMPLE_KB, KnowledgeBase.owner_id == user.id))
        return int(kb.id), data
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="仿真规程检索基线")
    parser.add_argument("--strictness", default="standard", choices=("loose", "standard", "strict"))
    parser.add_argument("--compare-strict", action="store_true", help="再用 strict 闸门跑一遍，只比较本地检索")
    parser.add_argument("--embedding-ref", default="", help="显式指定已配置的 embedding 模型引用。缺省则跳过")
    args = parser.parse_args()
    started = time.perf_counter()
    kb_id, data = _prepare()
    from youju.diag_eval import run_labeled

    report = run_labeled(kb_id, strictness=args.strictness)
    report["wall_ms"] = int((time.perf_counter() - started) * 1000)
    report["compare"] = None
    if args.compare_strict and args.strictness != "strict":
        other = run_labeled(kb_id, strictness="strict")
        report["compare"] = {
            "strictness": "strict",
            "embedding": "local:hash",
            "rerank": False,
            "latency_ms": other["latency_ms"],
            "metrics": other["metrics"],
        }
    embedding = {"status": "skipped", "reason": "未传入 --embedding-ref，未调用真实 embedding 或 rerank，也不记录虚构提升。"}
    if args.embedding_ref:
        embedding = {"status": "not_run", "reason": f"引用 {args.embedding_ref} 需要在已接入的模型服务上单独配置。本脚本的临时库没有供应商密钥，拒绝发起调用。", "ref": args.embedding_ref}
    report["embedding_compare"] = embedding
    print(f"数据目录：{data}")
    print(f"闸门 {report['strictness']} · 嵌入 {report['embedding']} · 重排 {report['rerank']} · 检索耗时 {report['latency_ms']} ms")
    print(report["metrics"]["note"])
    for name, item in report["metrics"].items():
        if name == "note" or not isinstance(item, dict):
            continue
        rate = "n/a" if item["rate"] is None else f"{item['rate']:.0%}"
        print(f"{name:<18} {item['hit']}/{item['denom']}  {rate}")
    if report["compare"]:
        print("对照 strict 闸门（同一套本地哈希向量 + BM25）：")
        for name, item in report["compare"]["metrics"].items():
            if name == "note" or not isinstance(item, dict):
                continue
            rate = "n/a" if item["rate"] is None else f"{item['rate']:.0%}"
            print(f"  strict {name:<16} {item['hit']}/{item['denom']}  {rate}")
    print(embedding["reason"])
    print("BASELINE_JSON " + json.dumps({k: report[k] for k in ("strictness", "embedding", "rerank", "latency_ms", "metrics", "compare", "embedding_compare")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
