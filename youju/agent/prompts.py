"""提示词和问题改写。智能体自己的人设写在 system_prompt 字段，这里只放通用规则。"""

from __future__ import annotations

import json
import re
from datetime import datetime

from youju.agent.citations import strip_markers
from youju.textproc import is_cjk

RULES = {
    "rag": (
        "## 回答规则\n"
        "1. 只依据「参考资料」回答。资料没有覆盖的内容，直接说明资料中没有相关依据，不要猜测，也不要用常识补全结论。\n"
        "2. 每个关键结论后用方括号标注来源编号，例如：……需要立即停止作业[1]。多个来源写成[1][2]。\n"
        "3. 只能使用参考资料里出现过的编号。\n"
        "4. 使用与用户相同的语言作答；先给结论，步骤类内容用有序列表。"
    ),
    "agent": (
        "## 工作方式\n"
        "1. 回答事实性问题前先调用 search_knowledge 检索；资料不够时换关键词再检索，最多 3 次。\n"
        "2. 只依据检索结果作答，每个关键结论后标注检索结果里的编号，例如：……[2]。\n"
        "3. 检索不到依据时，明确说明资料中没有相关依据，不要编造。\n"
        "4. 使用与用户相同的语言作答；先给结论，步骤类内容用有序列表。"
    ),
    "chat": "使用与用户相同的语言作答，准确、简洁；不确定的内容要明确说明不确定。",
}

SMALLTALK = "用户在寒暄或询问你能做什么。简短友好地回应，介绍你能基于哪些资料回答什么问题，不要编造资料内容。"

LEVELS = {"critical": "紧急", "high": "高", "medium": "中", "low": "低", "info": "提示"}

_REFER = ("这个", "那个", "它", "其", "上面", "刚才", "继续", "还有", "那么", "如果", "这种", "该")
_GREET = ("你好", "您好", "hello", "hi", "嗨", "在吗", "谢谢", "你是谁", "你能做什么", "你可以做什么", "介绍一下你", "help")


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def system_prompt(agent, mode: str, kb_names: list[str]) -> str:
    base = (agent.system_prompt or "").strip() or f"你是「{agent.name}」。"
    if kb_names:
        base += f"\n\n可用知识库：{'、'.join(kb_names)}。"
    rules = RULES["chat"] if not kb_names else RULES.get(mode, RULES["rag"])
    return f"{base}\n\n{rules}\n\n当前时间：{now_text()}。"


def rag_message(question: str, book, evidence: bool, strict: bool) -> str:
    if not len(book):
        return f"参考资料：无。\n\n问题：{question}"
    note = ""
    if not evidence and not strict:
        note = (
            "\n\n（这些资料和问题的相关度较低。如果不能据此回答，请先说明「资料库中没有直接依据」，"
            "再给出谨慎的通用建议，通用建议部分不要标注编号。）"
        )
    return f"参考资料：\n\n{book.context()}{note}\n\n---\n问题：{question}"


def history(items: list[dict], limit: int = 6) -> list[dict]:
    out = []
    for msg in items[-limit:]:
        if msg.get("role") not in ("user", "assistant"):
            continue
        content = strip_markers(str(msg.get("content") or "")).strip()
        if content:
            out.append({"role": msg["role"], "content": content[:1500]})
    while out and out[0]["role"] != "user":
        out.pop(0)
    return out


def search_query(question: str, history_items: list[dict]) -> str:
    """多轮追问时，把上一个问题拼进检索词，避免「那这个呢」检索不到东西。"""
    question = question.strip()
    previous = next((m["content"] for m in reversed(history_items) if m.get("role") == "user"), "")
    if previous and (len(question) <= 12 or any(word in question for word in _REFER)):
        return f"{previous} {question}"[:400]
    return question[:400]


def is_smalltalk(question: str) -> bool:
    text = question.strip().lower().strip("！!。.？?~～ ")
    return len(text) <= 12 and any(text == g or text.startswith(g) for g in _GREET)


def level_name(level: str) -> str:
    return LEVELS.get((level or "").lower(), level or "未分级")


def event_question(event: dict) -> str:
    data = json.dumps(event.get("data"), ensure_ascii=False, indent=2) if event.get("data") is not None else "无"
    return (
        "收到一条业务事件，请依据资料给出处置建议。\n\n"
        f"事件：{event.get('title') or '未命名事件'}\n"
        f"级别：{level_name(event.get('level') or '')}\n"
        f"来源：{event.get('source') or '未知'}\n"
        f"数据：\n{data}\n\n"
        "请按以下结构输出：\n"
        "### 事件摘要\n### 处置步骤\n（每一步标注依据编号）\n### 需要人工确认\n"
        "如果资料中没有对应规程，写明「未检索到对应规程」，不要编造处置步骤。"
    )


_DATE = re.compile(r"^\s*\d{2,4}[-/年.]\d{1,2}")


def _descriptive(value: str) -> bool:
    """事件字段里有描述性的中文短语才拿来检索，点位编号、时间戳这类会稀释检索词。"""
    text = value.strip()
    return sum(1 for ch in text if is_cjk(ch)) >= 4 and len(text) <= 60 and not _DATE.match(text)


def event_queries(event: dict) -> list[str]:
    found = [str(event.get("title") or "").strip()]

    def walk(value) -> None:
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, str) and _descriptive(value):
            found.append(value.strip())

    walk(event.get("data"))
    return list(dict.fromkeys(q for q in found if q))[:7]


def event_query(event: dict) -> str:
    return " ".join(event_queries(event))[:300]
