"""一次回答的编排：选模式 → 检索或调用工具 → 生成 → 引用核验。

模式：
- rag：先检索再生成，任何对话模型都能用
- agent：模型通过 Function Calling 自己决定检索什么、检索几次
- chat：没有绑定知识库的通用对话
- offline：没有接入模型，直接摘录原文
- fallback：模型调用失败，降级成离线摘录
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass, field

from sqlalchemy import select

from youju.agent import prompts
from youju.agent.citations import EMPTY_CHECK, normalize, strip_markers, used_numbers, verify
from youju.agent.offline import extractive_answer, extractive_event, greeting
from youju.agent.sources import SourceBook
from youju.agent.tools import TOOL_TITLES, ToolBox, tool_specs
from youju.db import SessionLocal
from youju.models import Agent, KnowledgeBase
from youju.providers.base import ProviderError
from youju.providers.registry import ModelInfo, resolve_chat
from youju.rag.retrieve import retrieve
from youju.textproc import estimate_tokens

log = logging.getLogger("youju.agent")

MAX_TOOL_ROUNDS = 4
REFUSAL = (
    "资料库中没有找到与这个问题直接相关的依据。为避免误导，这个问题暂不作答。\n\n"
    "可以换个说法、补充更具体的关键词，或联系管理员补充资料。"
)
EVENT_REFUSAL = "未检索到对应规程，不能生成处置结论，请人工研判。"
NO_MODEL = "当前还没有接入大模型，这个智能体也没有绑定知识库，暂时无法回答。管理员可以在「设置 → 模型服务」中接入模型。"


@dataclass
class AgentSnapshot:
    id: int
    name: str
    emoji: str = "✨"
    system_prompt: str = ""
    opening: str = ""
    suggestions: list[str] = field(default_factory=list)
    kb_ids: list[int] = field(default_factory=list)
    model_ref: str | None = None
    strategy: str = "rag"
    strict: bool = True
    strictness: str = "standard"
    top_k: int = 5
    temperature: float = 0.3

    @classmethod
    def of(cls, agent: Agent) -> "AgentSnapshot":
        return cls(
            id=agent.id,
            name=agent.name,
            emoji=agent.emoji,
            system_prompt=agent.system_prompt or "",
            opening=agent.opening or "",
            suggestions=list(agent.suggestions or []),
            kb_ids=[int(k) for k in agent.kb_ids or []],
            model_ref=agent.model_ref,
            strategy=agent.strategy or "rag",
            strict=bool(agent.strict),
            strictness=agent.strictness or "standard",
            top_k=int(agent.top_k or 5),
            temperature=float(agent.temperature if agent.temperature is not None else 0.3),
        )


@dataclass
class RunInput:
    agent: AgentSnapshot
    question: str
    history: list[dict] = field(default_factory=list)
    model_ref: str | None = None
    event: dict | None = None
    mcp_servers: list[dict] = field(default_factory=list)


class Run:
    def __init__(self, inp: RunInput, info: ModelInfo, mode: str):
        self.inp = inp
        self.info = info
        self.mode = mode
        self.book = SourceBook()
        self.trace: list[dict] = []
        self.usage = {"input": 0, "output": 0, "estimated": False}
        self.started = time.perf_counter()
        self.text = ""
        self.reasoning = ""
        self.refused = False
        self.error: str | None = None
        self.turn_text = ""
        self.turn_reasoning = ""
        self.turn_calls: list = []
        self.turn_raw = None
        self._clock: dict[str, float] = {}

    def start(self, kind: str, title: str, **extra) -> dict:
        step = {"id": f"s{len(self.trace) + 1}", "kind": kind, "title": title, "status": "running", "detail": ""}
        step.update({k: v for k, v in extra.items() if v is not None})
        self.trace.append(step)
        self._clock[step["id"]] = time.perf_counter()
        return step

    def end(self, step: dict, status: str = "done", detail: str = "", **extra) -> dict:
        step["status"] = status
        step["detail"] = detail
        step.update({k: v for k, v in extra.items() if v is not None})
        began = self._clock.pop(step["id"], time.perf_counter())
        step["elapsed_ms"] = int((time.perf_counter() - began) * 1000)
        return step

    @staticmethod
    def event(step: dict) -> dict:
        return {"type": "step", "step": dict(step)}

    def usage_text(self) -> str:
        tail = "（估算）" if self.usage["estimated"] else ""
        return f"输入 {self.usage['input']} · 输出 {self.usage['output']} tokens{tail}"


def _mode(provider, info: ModelInfo, agent: AgentSnapshot, kbs: list) -> str:
    if provider is None:
        return "offline"
    if not kbs:
        return "chat"
    if agent.strategy == "agent" and info.tools:
        return "agent"
    return "rag"


async def run(inp: RunInput) -> AsyncIterator[dict]:
    db = SessionLocal()
    try:
        provider, info = resolve_chat(db, inp.model_ref, inp.agent.model_ref)
        rows = db.scalars(select(KnowledgeBase).where(KnowledgeBase.id.in_(inp.agent.kb_ids or [-1]))).all()
        names = {kb.id: kb.name for kb in rows}
    finally:
        db.close()
    kbs = [(kid, names[kid]) for kid in inp.agent.kb_ids if kid in names]
    r = Run(inp, info, _mode(provider, info, inp.agent, kbs))
    yield {"type": "meta", "mode": r.mode, "model": info.public()}
    try:
        flow = _agent(r, provider, kbs) if r.mode == "agent" else _pipeline(r, provider, kbs)
        async for ev in flow:
            yield ev
    except ProviderError as exc:
        r.error = str(exc)
        yield {"type": "error", "message": r.error}
    except Exception as exc:  # noqa: BLE001
        log.exception("agent run failed")
        r.error = f"内部错误：{exc.__class__.__name__}"
        yield {"type": "error", "message": r.error}
    yield _final(r)


async def collect(inp: RunInput) -> dict:
    final: dict = {}
    async for ev in run(inp):
        if ev["type"] == "final":
            final = ev
    return final


async def _type_out(r: Run, text: str, size: int = 8) -> AsyncIterator[dict]:
    r.text = text
    for start in range(0, len(text), size):
        yield {"type": "delta", "delta": text[start : start + size]}
        await asyncio.sleep(0.006)


async def _turn(r: Run, provider, messages: list[dict], tools=None, tool_choice: str | None = None) -> AsyncIterator[dict]:
    r.turn_text, r.turn_reasoning, r.turn_calls, r.turn_raw = "", "", [], None
    usage = None
    stream = provider.stream(
        r.info.model, messages, tools=tools, temperature=r.inp.agent.temperature, tool_choice=tool_choice
    )
    async with aclosing(stream) as parts:
        async for part in parts:
            if part.kind == "text":
                r.turn_text += part.text
                yield {"type": "delta", "delta": part.text}
            elif part.kind == "reasoning":
                r.turn_reasoning += part.text
                r.reasoning += part.text
                yield {"type": "reasoning", "delta": part.text}
            elif part.kind == "tool_calls":
                r.turn_calls = list(part.calls)
                if part.raw is not None:
                    r.turn_raw = part.raw
            elif part.kind == "usage":
                usage = part.usage
            elif part.kind == "end" and part.raw is not None:
                r.turn_raw = part.raw
    if usage:
        r.usage["input"] += int(usage.get("input") or 0)
        r.usage["output"] += int(usage.get("output") or 0)
    else:
        r.usage["input"] += estimate_tokens("".join(str(m.get("content") or "") for m in messages))
        r.usage["output"] += estimate_tokens(r.turn_text + r.turn_reasoning)
        r.usage["estimated"] = True


async def _pipeline(r: Run, provider, kbs: list[tuple[int, str]]) -> AsyncIterator[dict]:
    inp, agent = r.inp, r.inp.agent
    smalltalk = inp.event is None and prompts.is_smalltalk(inp.question)
    evidence = False
    if kbs and not smalltalk and not len(r.book):
        query = prompts.event_query(inp.event) if inp.event else prompts.search_query(inp.question, inp.history)
        step = r.start("search", "检索 " + "、".join(name for _, name in kbs), query=query)
        yield r.event(step)
        result = await asyncio.to_thread(retrieve, [kid for kid, _ in kbs], query, agent.top_k, agent.strictness)
        evidence = result.evidence
        if evidence or not agent.strict:
            for hit in result.hits:
                r.book.add(hit)
        detail = f"命中 {len(result.hits)} 条 · {result.reason}"
        if result.notes:
            detail += " · " + "；".join(result.notes)
        yield r.event(r.end(step, detail=detail, hits=len(result.hits), evidence=evidence))
        if len(r.book):
            yield {"type": "sources", "sources": r.book.items}
        if not evidence and agent.strict and not prompts.is_config_request(inp.question or ""):
            r.refused = True
            guard = r.start("guard", "没有足够依据，按严格模式拒答")
            yield r.event(r.end(guard))
            async for ev in _type_out(r, EVENT_REFUSAL if inp.event else REFUSAL):
                yield ev
            return
    elif len(r.book):
        evidence = True

    if provider is None:
        async for ev in _offline(r, kbs, smalltalk):
            yield ev
        return

    with_sources = bool(kbs) and not smalltalk
    system = prompts.system_prompt(agent, "rag", [n for _, n in kbs] if with_sources else [])
    if smalltalk:
        system += "\n\n" + prompts.SMALLTALK
    elif prompts.is_config_request(inp.question or ""):
        system += "\n\n当前问题是配置协助，不要因资料不足而整段拒答。"
    question = prompts.event_question(inp.event) if inp.event else inp.question
    content = prompts.rag_message(question, r.book, evidence, agent.strict) if with_sources else question
    messages = [{"role": "system", "content": system}, *prompts.history(inp.history), {"role": "user", "content": content}]
    step = r.start("generate", f"生成回答 · {r.info.model}")
    yield r.event(step)
    try:
        async for ev in _turn(r, provider, messages):
            yield ev
    except ProviderError as exc:
        yield r.event(r.end(step, status="error", detail=str(exc)))
        if r.turn_text or not kbs:
            raise
        async for ev in _fallback(r, kbs, exc):
            yield ev
        return
    r.text = r.turn_text
    yield r.event(r.end(step, detail=r.usage_text()))


async def _offline(r: Run, kbs: list, smalltalk: bool) -> AsyncIterator[dict]:
    inp = r.inp
    title = "离线摘录原文（模型不可用）" if r.mode == "fallback" else "离线摘录原文（未接入大模型）"
    step = r.start("generate", title)
    yield r.event(step)
    if smalltalk:
        text = greeting(inp.agent)
    elif not kbs:
        text = NO_MODEL
    elif not len(r.book):
        text = EVENT_REFUSAL if inp.event else REFUSAL
        r.refused = True
    elif inp.event:
        text = extractive_event(inp.event, r.book)
    else:
        text = extractive_answer(inp.question, r.book)
    yield r.event(r.end(step, detail="直接摘录检索到的原文句子"))
    async for ev in _type_out(r, text):
        yield ev


async def _fallback(r: Run, kbs: list, exc: ProviderError) -> AsyncIterator[dict]:
    r.mode = "fallback"
    r.error = str(exc)
    note = r.start("fallback", "模型调用失败，改用离线摘录")
    yield r.event(r.end(note, status="done", detail=str(exc)[:200]))
    async for ev in _pipeline(r, None, kbs):
        yield ev


async def _agent(r: Run, provider, kbs: list[tuple[int, str]]) -> AsyncIterator[dict]:
    inp, agent = r.inp, r.inp.agent
    if inp.event is None and prompts.is_smalltalk(inp.question):
        async for ev in _pipeline(r, provider, kbs):
            yield ev
        return
    names = [name for _, name in kbs]
    toolbox = ToolBox(kbs, r.book, agent.top_k, agent.strictness, mcp_servers=list(inp.mcp_servers or []))
    specs = tool_specs(names, mcp=bool(inp.mcp_servers))
    question = prompts.event_question(inp.event) if inp.event else inp.question
    system = prompts.system_prompt(agent, "agent", names)
    if inp.mcp_servers:
        lines = ["已连接外部 MCP。需要时调用 use_mcp，server 填服务名，tool 填工具名。"]
        for server in inp.mcp_servers:
            names_ = "、".join(t.get("name") or "" for t in server.get("tools") or []) or "尚未拉到工具"
            lines.append(f"- {server.get('name')}: {names_}")
        system += "\n\n" + "\n".join(lines)
    messages = [
        {"role": "system", "content": system},
        *prompts.history(inp.history),
        {"role": "user", "content": question},
    ]
    for round_no in range(MAX_TOOL_ROUNDS + 1):
        last = round_no == MAX_TOOL_ROUNDS
        step = r.start("think", "整理最终回答" if last else f"模型规划 · 第 {round_no + 1} 轮")
        yield r.event(step)
        try:
            async for ev in _turn(r, provider, messages, specs, "none" if last else None):
                yield ev
        except ProviderError as exc:
            yield r.event(r.end(step, status="error", detail=str(exc)))
            if r.turn_text:
                raise
            if round_no == 0 and exc.status == 400 and "tool" in str(exc).lower():
                r.mode = "rag"
                note = r.start("fallback", "模型不支持工具调用，改用检索增强")
                yield r.event(r.end(note))
                async for ev in _pipeline(r, provider, kbs):
                    yield ev
                return
            async for ev in _fallback(r, kbs, exc):
                yield ev
            return
        if r.turn_calls and not last:
            said = r.turn_text.strip()
            if said:
                yield {"type": "interim_reset"}
            yield r.event(r.end(step, detail=f"调用 {len(r.turn_calls)} 个工具" + (f" · {said[:60]}" if said else "")))
            messages.append(
                {
                    "role": "assistant",
                    "content": r.turn_text,
                    "tool_calls": r.turn_calls,
                    "raw": r.turn_raw,
                    "reasoning": r.turn_reasoning,
                }
            )
            before = len(r.book)
            for call in r.turn_calls:
                tool_step = r.start("tool", TOOL_TITLES.get(call.name, call.name), tool=call.name, arguments=call.arguments)
                yield r.event(tool_step)
                text, meta = await toolbox.run(call)
                yield r.event(
                    r.end(
                        tool_step,
                        status="error" if meta.get("error") else "done",
                        detail=meta.get("detail", ""),
                        hits=meta.get("hits"),
                        evidence=meta.get("evidence"),
                    )
                )
                messages.append({"role": "tool", "tool_call_id": call.id, "name": call.name, "content": text})
            if len(r.book) > before:
                yield {"type": "sources", "sources": r.book.items}
            continue
        r.text = r.turn_text
        yield r.event(r.end(step, detail=r.usage_text()))
        break
    if agent.strict and not toolbox.evidence and not prompts.is_config_request(inp.question or ""):
        r.refused = True
        r.text = EVENT_REFUSAL if inp.event else REFUSAL
        guard = r.start("guard", "没有检索到足够依据，已替换为拒答")
        yield r.event(r.end(guard))
        yield {"type": "replace", "content": r.text}


def _final(r: Run) -> dict:
    valid = r.book.numbers()
    question = prompts.event_query(r.inp.event) if r.inp.event else r.inp.question
    if valid and r.text and not r.refused:
        check = verify(r.text, r.book, question)
    else:
        check = dict(EMPTY_CHECK)
    content = normalize(r.text, valid) if valid else strip_markers(r.text)
    return {
        "type": "final",
        "content": content,
        "reasoning": r.reasoning,
        "sources": r.book.items,
        "used": used_numbers(content),
        "check": check,
        "refused": r.refused,
        "mode": r.mode,
        "model": r.info.public(),
        "usage": r.usage,
        "latency_ms": int((time.perf_counter() - r.started) * 1000),
        "trace": [dict(step) for step in r.trace],
        "error": r.error,
    }
