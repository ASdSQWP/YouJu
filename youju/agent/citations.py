"""引用标注的规整和核验。

核验不再调用模型：把每句话的有效词和它引用的原文做覆盖率比较，
低于 0.3 判为依据不足。复述问题、拒答和客套话不计入结论。
"""

from __future__ import annotations

import re
from collections import Counter

from youju.textproc import key_terms, sentences

MARK = re.compile(r"\[(\d{1,3}(?:\s*[,，、]\s*\d{1,3})*)\]|【(\d{1,3})】|\[\^(\d{1,3})\]")
_CODE = re.compile(r"```[\s\S]*?(?:```|$)|`[^`\n]+`")
_ONLY_MARKS = re.compile(r"^[\s。.；;，,]*(?:(?:\[\d{1,3}(?:\s*[,，、]\s*\d{1,3})*\]|【\d{1,3}】|\[\^\d{1,3}\])[\s。.；;，,]*)+$")

SUPPORT_MIN = 0.3
_META = ("仅供参考", "如有疑问", "请咨询", "建议联系", "以上", "希望对", "需要我", "可以继续", "如需", "人工确认", "请结合")
_REFUSE = ("没有相关", "没有找到", "未检索到", "无法回答", "资料中没有", "未找到", "暂不作答", "没有直接", "not found", "no relevant")


def _numbers(raw: str) -> list[int]:
    return [int(x) for x in re.split(r"\s*[,，、]\s*", raw) if x.strip().isdigit()]


def _outside_code(text: str, fn) -> str:
    out, last = [], 0
    for match in _CODE.finditer(text):
        out.append(fn(text[last : match.start()]))
        out.append(match.group(0))
        last = match.end()
    out.append(fn(text[last:]))
    return "".join(out)


def normalize(text: str, valid: set[int]) -> str:
    """统一成 [n] 形式，去掉不存在的编号。"""

    def fix(segment: str) -> str:
        def repl(match: re.Match) -> str:
            raw = match.group(1) or match.group(2) or match.group(3) or ""
            return "".join(f"[{n}]" for n in _numbers(raw) if n in valid)

        return MARK.sub(repl, segment)

    return _outside_code(text or "", fix)


def strip_markers(text: str) -> str:
    return _outside_code(text or "", lambda segment: MARK.sub("", segment))


def used_numbers(text: str) -> list[int]:
    found: list[int] = []
    plain = _CODE.sub("", text or "")
    for match in MARK.finditer(plain):
        for n in _numbers(match.group(1) or match.group(2) or match.group(3) or ""):
            if n not in found:
                found.append(n)
    return found


def _marks(unit: str) -> list[int]:
    nums: list[int] = []
    for match in MARK.finditer(unit):
        nums.extend(_numbers(match.group(1) or match.group(2) or match.group(3) or ""))
    return nums


def _units(text: str) -> list[tuple[str, list[int]]]:
    """按段落切句。段内没标注的句子沿用后面最近一句的编号：模型常把引用放在段尾。"""
    out: list[tuple[str, list[int]]] = []
    for line in _CODE.sub("", text or "").splitlines():
        units: list[str] = []
        for sentence in sentences(line):
            if _ONLY_MARKS.match(sentence) and units:
                units[-1] += sentence
            else:
                units.append(sentence)
        carry: list[int] = []
        tagged = []
        for unit in reversed(units):
            own = _marks(unit)
            if own:
                carry = own
            tagged.append((unit, own or carry))
        out.extend(reversed(tagged))
    return out


def verify(answer: str, book, question: str = "") -> dict:
    valid = book.numbers()
    asked = set(key_terms(question))
    claims: list[dict] = []
    for unit, nums in _units(answer):
        plain = MARK.sub("", unit).strip().lstrip("-*•0123456789.、） )").strip()
        if not plain or plain.startswith("#") or plain.endswith(("：", ":")):
            continue
        words = set(key_terms(plain))
        if not nums:
            if len(words) < 6 or any(m in plain for m in _META) or any(r in plain.lower() for r in _REFUSE):
                continue
            if asked and len(words & asked) >= 0.7 * len(words):
                continue
            claims.append({"text": plain[:160], "nums": [], "status": "uncited", "support": 0.0})
            continue
        good = [n for n in nums if n in valid]
        if not good:
            claims.append({"text": plain[:160], "nums": nums, "status": "invalid", "support": 0.0})
            continue
        target: set[str] = set()
        for n in good:
            target |= book.token_set(n)
        support = len(words & target) / len(words) if words else 1.0
        claims.append(
            {
                "text": plain[:160],
                "nums": good,
                "status": "supported" if support >= SUPPORT_MIN else "weak",
                "support": round(support, 2),
            }
        )
    counts = Counter(c["status"] for c in claims)
    total = len(claims)
    if total == 0:
        verdict = "none"
    elif counts["invalid"] or counts["supported"] == 0:
        verdict = "fail"
    elif counts["weak"] or counts["uncited"]:
        verdict = "warn"
    else:
        verdict = "pass"
    return {
        "verdict": verdict,
        "total": total,
        "supported": counts["supported"],
        "weak": counts["weak"],
        "uncited": counts["uncited"],
        "invalid": counts["invalid"],
        "claims": claims[:30],
    }


EMPTY_CHECK = {"verdict": "none", "total": 0, "supported": 0, "weak": 0, "uncited": 0, "invalid": 0, "claims": []}
