"""智能体模式下模型可以调用的工具。"""

from __future__ import annotations

import asyncio
import logging

from youju.agent.prompts import now_text
from youju.agent.sources import SourceBook
from youju.providers.base import ToolCall, ToolSpec
from youju.rag.retrieve import neighbors, retrieve

log = logging.getLogger("youju.tools")

TOOL_TITLES = {"search_knowledge": "检索知识库", "read_more": "查看相邻原文", "current_time": "获取当前时间"}
MAX_SEARCHES = 5


def tool_specs(kb_names: list[str]) -> list[ToolSpec]:
    search_props: dict = {"query": {"type": "string", "description": "检索词或完整问题，尽量包含关键名词"}}
    if len(kb_names) > 1:
        search_props["knowledge_base"] = {
            "type": "string",
            "description": "只想在某个知识库里检索时填写",
            "enum": kb_names,
        }
    return [
        ToolSpec(
            "search_knowledge",
            f"在知识库（{'、'.join(kb_names)}）中检索资料，返回带编号的原文片段。回答事实性问题前必须先检索。",
            {"type": "object", "properties": search_props, "required": ["query"]},
        ),
        ToolSpec(
            "read_more",
            "查看某个来源编号前后相邻的原文，用于补全被截断的步骤或条款。",
            {
                "type": "object",
                "properties": {"source": {"type": "integer", "description": "来源编号，例如 2"}},
                "required": ["source"],
            },
        ),
        ToolSpec("current_time", "获取当前日期和时间。", {"type": "object", "properties": {}}),
    ]


class ToolBox:
    def __init__(self, kbs: list[tuple[int, str]], book: SourceBook, top_k: int, strictness: str):
        self.kbs = kbs
        self.book = book
        self.top_k = top_k
        self.strictness = strictness
        self.evidence = False
        self.searches = 0

    async def run(self, call: ToolCall) -> tuple[str, dict]:
        try:
            if call.name == "search_knowledge":
                return await self._search(call.arguments)
            if call.name == "read_more":
                return await self._read_more(call.arguments)
            if call.name == "current_time":
                text = now_text()
                return text, {"detail": text}
        except Exception as exc:  # noqa: BLE001
            log.exception("tool %s failed", call.name)
            return f"工具执行出错：{exc}", {"error": True, "detail": str(exc)[:200]}
        return f"没有名为 {call.name} 的工具。", {"error": True, "detail": "未知工具"}

    async def _search(self, args: dict) -> tuple[str, dict]:
        query = str(args.get("query") or "").strip()
        if not query:
            return "query 不能为空。", {"error": True, "detail": "检索词为空"}
        self.searches += 1
        if self.searches > MAX_SEARCHES:
            return "检索次数已达上限，请根据已有资料作答。", {"error": True, "detail": "检索次数超限"}
        target = str(args.get("knowledge_base") or "").strip()
        ids = [kid for kid, name in self.kbs if name == target] or [kid for kid, _ in self.kbs]
        result = await asyncio.to_thread(retrieve, ids, query, self.top_k, self.strictness)
        self.evidence = self.evidence or result.evidence
        if not result.hits:
            return f"检索「{query}」没有找到相关资料。", {"detail": f"「{query}」没有命中", "hits": 0, "evidence": False}
        numbers = [self.book.add(hit) for hit in result.hits]
        head = f"检索「{query}」命中 {len(numbers)} 条" + ("" if result.evidence else "（相关度较低，谨慎使用）")
        return (
            f"{head}：\n\n{self.book.context(numbers)}",
            {
                "detail": f"「{query}」命中 {len(numbers)} 条 · {result.reason}",
                "hits": len(numbers),
                "evidence": result.evidence,
            },
        )

    async def _read_more(self, args: dict) -> tuple[str, dict]:
        try:
            number = int(args.get("source"))
        except (TypeError, ValueError):
            return "source 必须是来源编号。", {"error": True, "detail": "参数错误"}
        item = self.book.get(number)
        if item is None:
            return f"没有编号为 {number} 的来源。", {"error": True, "detail": f"来源 {number} 不存在"}
        hits = await asyncio.to_thread(neighbors, item["chunk_id"])
        numbers = [self.book.add(hit) for hit in hits]
        if not numbers:
            return "这个来源前后没有更多原文。", {"detail": "没有相邻片段"}
        return "相邻原文：\n\n" + self.book.context(numbers), {"detail": f"来源 [{number}] 前后 {len(numbers)} 段"}
