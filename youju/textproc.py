"""中英文混合切词和句子切分。检索、引用核验和离线抽取共用这一套。"""

from __future__ import annotations

import re
from collections.abc import Iterable

STOP = frozenset(
    {
        "什么", "怎么", "如何", "为什", "哪些", "哪个", "是否", "请问", "一下", "这个", "那个",
        "怎样", "么样", "么办", "么处", "可以", "能否", "是不", "不是", "现在", "应该", "需要",
        "我们", "你们", "他们", "以及", "或者", "还是", "就是", "的话", "多少", "多久", "时候",
        "the", "a", "an", "is", "are", "was", "be", "to", "of", "and", "or", "in", "on", "for",
        "with", "what", "how", "why", "can", "do", "does", "we", "you", "it", "this", "that",
        "please", "should",
    }
)

# 语气词、代词和疑问字组成的二字多半是跨词碎片（如「退吗」「带没」），不当作有效词
FUNC_CHARS = frozenset("吗呢吧啊呀嘛么的了我你他她它们这那哪谁怎什啥没")

# 先把整个功能词换成空格，避免「好可」「以继」这类跨词二字
_FUNCTION_WORDS = re.compile(
    "可不可以|能不能|是不是|有没有|为什么|怎么样|怎么办|怎么|什么|可以|是否|能否|多少|多久|哪些|哪个|哪里|一下|请问|如何"
)

_SPLIT = re.compile(r"[^。！？!?；;]+[。！？!?；;]*")


def is_cjk(ch: str) -> bool:
    code = ord(ch)
    return 0x4E00 <= code <= 0x9FFF or 0x3400 <= code <= 0x4DBF or 0xF900 <= code <= 0xFAFF


def tokens(text: str) -> list[str]:
    """汉字取相邻二字，英文和数字按词。"""
    out: list[str] = []
    run: list[str] = []
    word: list[str] = []

    def flush_run() -> None:
        if len(run) == 1:
            out.append(run[0])
        elif run:
            out.extend(run[i] + run[i + 1] for i in range(len(run) - 1))
        run.clear()

    def flush_word() -> None:
        if word:
            out.append("".join(word).lower())
            word.clear()

    for ch in text or "":
        if is_cjk(ch):
            flush_word()
            run.append(ch)
        elif ch.isalnum():
            flush_run()
            word.append(ch)
        else:
            flush_run()
            flush_word()
    flush_run()
    flush_word()
    return out


def informative(toks: Iterable[str]) -> list[str]:
    out = []
    for t in toks:
        if t in STOP:
            continue
        if is_cjk(t[0]):
            if any(ch in FUNC_CHARS for ch in t):
                continue
        elif len(t) < 2:
            continue
        out.append(t)
    return out


def key_terms(text: str) -> list[str]:
    """问题或结论里真正有区分度的词，用于覆盖率和引用核验。"""
    return informative(tokens(_FUNCTION_WORDS.sub(" ", text or "")))


def sentences(text: str) -> list[str]:
    out: list[str] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        for match in _SPLIT.finditer(line):
            piece = match.group(0).strip()
            if piece:
                out.append(piece)
    return out


def estimate_tokens(text: str) -> int:
    cjk = sum(1 for ch in text if is_cjk(ch))
    return cjk + max(0, len(text) - cjk) // 4
