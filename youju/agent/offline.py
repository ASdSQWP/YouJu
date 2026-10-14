"""没有接入大模型时，从检索到的原文里挑句子作答，每句都带编号。"""

from __future__ import annotations

import math
import re
from collections import Counter

from youju.agent.prompts import event_query, level_name
from youju.agent.sources import SourceBook
from youju.textproc import key_terms, sentences, tokens

_DIRECTIVE = ("必须", "禁止", "不得", "应当", "立即", "严禁", "需要", "需", "应", "先", "优先", "可以", "可")
_ENUM = re.compile(r"^\s*(\d+[.、)）]|[-*•])\s*")


def _pick(query: str, book: SourceBook, limit: int) -> list[tuple[str, int]]:
    """按 IDF 加权的词重合挑句子，再乘上所在切片的检索相关度，避免「安全帽」蹭「安全带」的分。"""
    wanted = set(key_terms(query))
    pool = []
    best_cov = max((item.get("scores", {}).get("coverage") or 0.0 for item in book.items), default=0.0)
    for rank, item in enumerate(book.items):
        cov = item.get("scores", {}).get("coverage")
        relevance = (cov / best_cov) if cov is not None and best_cov > 0 else 1.0 / (rank + 1)
        for order, sentence in enumerate(sentences(item["text"])):
            sentence = _ENUM.sub("", sentence).strip()
            if len(sentence) >= 8:
                pool.append((rank, order, sentence, item["n"], set(tokens(sentence)), relevance))
    if not pool or not wanted:
        return [(row[2], row[3]) for row in pool[:1]]
    df = Counter(t for row in pool for t in row[4] & wanted)
    weight = {t: math.log(1 + len(pool) / (1 + df.get(t, 0))) for t in wanted}
    total = sum(weight.values()) or 1.0
    scored = []
    for rank, order, sentence, number, words, relevance in pool:
        overlap = sum(weight[t] for t in wanted & words) / total
        if overlap <= 0:
            continue
        bonus = 0.06 if any(word in sentence for word in _DIRECTIVE) else 0.0
        scored.append((overlap * (0.4 + 0.6 * relevance) + bonus, rank, order, sentence, number))
    if not scored:
        return []
    scored.sort(key=lambda row: (-row[0], row[1], row[2]))
    floor = scored[0][0] * 0.6
    picked: list[tuple[str, int]] = []
    seen: set[str] = set()
    for score, _rank, _order, sentence, number in scored:
        key = sentence.rstrip("。；;.")
        if key in seen:
            continue
        if picked and score < floor:
            break
        seen.add(key)
        picked.append((sentence, number))
        if len(picked) >= limit:
            break
    return picked


def _cite(sentence: str, number: int) -> str:
    body = sentence.rstrip()
    if body and body[-1] in "。；;.!！?？":
        return f"{body[:-1]}[{number}]{body[-1]}"
    return f"{body}[{number}]"


def extractive_answer(question: str, book: SourceBook, limit: int = 4) -> str:
    picked = _pick(question, book, limit)
    if not picked:
        return "检索到的资料里没有能直接回答这个问题的句子。"
    lines = ["根据资料库原文，相关要点如下：", ""]
    lines += [f"{i}. {_cite(sentence, n)}" for i, (sentence, n) in enumerate(picked, start=1)]
    return "\n".join(lines)


def extractive_event(event: dict, book: SourceBook, limit: int = 5) -> str:
    picked = _pick(event_query(event), book, limit)
    lines = [
        "### 事件摘要",
        f"{event.get('title') or '未命名事件'}（级别：{level_name(event.get('level') or '')}）",
        "",
        "### 处置步骤",
    ]
    if picked:
        lines += [f"{i}. {_cite(sentence, n)}" for i, (sentence, n) in enumerate(picked, start=1)]
    else:
        lines.append("未检索到对应规程，请人工研判。")
    lines += ["", "### 需要人工确认", "以上步骤摘自规程原文，请值班人员结合现场情况确认后执行。"]
    return "\n".join(lines)


def greeting(agent) -> str:
    text = f"你好，我是{agent.name}。"
    if agent.opening:
        text += agent.opening
    if agent.suggestions:
        text += "\n\n可以试试这样问：\n" + "\n".join(f"- {item}" for item in agent.suggestions[:4])
    return text
