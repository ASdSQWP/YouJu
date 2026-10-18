"""智能体模式下模型可以调用的工具。"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import unicodedata

from youju.agent.prompts import now_text
from youju.agent.sources import SourceBook
from youju.providers.base import ToolCall, ToolSpec
from youju.rag.retrieve import neighbors, retrieve

log = logging.getLogger("youju.tools")

TOOL_TITLES = {"search_knowledge": "检索知识库", "read_more": "查看相邻原文", "current_time": "获取当前时间", "use_mcp": "调用外部 MCP"}
MAX_SEARCHES = 3
MAX_TOOL_CALLS = 9
MAX_TOOL_ARGUMENT_CHARS = 4_000
MAX_TOOL_RESULT_CHARS = 6_000
MAX_TOTAL_TOOL_RESULT_CHARS = 18_000
_RESULT_TRUNCATION = "\n\n[工具结果已截断]"


def tool_specs(kb_names: list[str], mcp: bool = False) -> list[ToolSpec]:
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
    ] + (
        [
            ToolSpec(
                "use_mcp",
                "调用用户已连接的外部 MCP 工具。server 填服务名称，tool 填工具名，arguments 为该工具的参数对象。",
                {
                    "type": "object",
                    "properties": {
                        "server": {"type": "string"},
                        "tool": {"type": "string"},
                        "arguments": {"type": "object"},
                    },
                    "required": ["server", "tool"],
                },
            )
        ]
        if mcp
        else []
    )


class ToolBox:
    def __init__(self, kbs: list[tuple[int, str]], book: SourceBook, top_k: int, strictness: str, mcp_servers: list[dict] | None = None):
        self.kbs = kbs
        self.book = book
        self.top_k = top_k
        self.strictness = strictness
        self.mcp_servers = mcp_servers or []
        self.evidence = False
        self.searches = 0
        self.search_queries: set[str] = set()
        self.tool_calls = 0
        self.result_chars = 0
        self.must_finalize = False

    async def run(self, call: ToolCall) -> tuple[str, dict]:
        self.tool_calls += 1
        if self.tool_calls > MAX_TOOL_CALLS:
            self.must_finalize = True
            return "工具调用总次数已达到上限，请根据已收集资料作答。", {
                "error": True,
                "detail": "工具调用次数超限",
            }
        if self.must_finalize:
            return "工具预算已耗尽，请根据已收集资料作答。", {
                "error": True,
                "detail": "工具预算已耗尽",
            }
        if self.result_chars >= MAX_TOTAL_TOOL_RESULT_CHARS:
            self.must_finalize = True
            return "工具结果总长度已达到上限，请根据现有资料作答。", {
                "error": True,
                "detail": "工具结果长度超限",
            }
        error = self._validate(call)
        if error:
            text = self._bounded_result(error)
            if self.tool_calls >= MAX_TOOL_CALLS:
                self.must_finalize = True
            return text, {"error": True, "detail": error[:200]}
        try:
            if call.name == "search_knowledge":
                text, meta = await self._search(call.arguments)
            elif call.name == "read_more":
                text, meta = await self._read_more(call.arguments)
            elif call.name == "current_time":
                text = now_text()
                meta = {"detail": text}
            else:  # use_mcp
                text, meta = await self._mcp(call.arguments)
        except Exception as exc:  # noqa: BLE001
            log.exception("tool %s failed", call.name)
            text, meta = f"工具执行出错：{exc}", {"error": True, "detail": str(exc)[:200]}
        text = self._bounded_result(text)
        if self.tool_calls >= MAX_TOOL_CALLS:
            self.must_finalize = True
        return text, meta

    def _validate(self, call: ToolCall) -> str:
        allowed = {"search_knowledge", "read_more", "current_time"}
        if self.mcp_servers:
            allowed.add("use_mcp")
        if not isinstance(call.name, str) or call.name not in allowed:
            return "工具名称无效或未声明，不能执行。"
        if not isinstance(call.arguments, dict):
            return "工具参数必须是 JSON 对象。"
        if call.raw_arguments is not None and not isinstance(call.raw_arguments, str):
            return "工具参数格式无效，已拒绝执行。"
        try:
            raw = call.raw_arguments or json.dumps(call.arguments, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            return "工具参数无法序列化，已拒绝执行。"
        if len(raw) > MAX_TOOL_ARGUMENT_CHARS:
            return "工具参数过长，已拒绝执行。"
        args = call.arguments
        if "_invalid" in args:
            return "工具参数不是有效的 JSON 对象，请修正后重试。"

        if call.name == "search_knowledge":
            if not self.kbs:
                return "当前智能体没有绑定知识库，不能检索。"
            if set(args) - {"query", "knowledge_base"}:
                return "search_knowledge 含有未声明的参数。"
            query = args.get("query")
            if not isinstance(query, str) or not query.strip():
                return "query 必须是非空字符串。"
            if len(query) > 400:
                return "query 不能超过 400 个字符。"
            target = args.get("knowledge_base")
            if target is not None:
                if not isinstance(target, str) or len(target) > 80:
                    return "knowledge_base 必须是有效的知识库名称。"
                if target not in {name for _, name in self.kbs}:
                    return f"知识库「{target}」未绑定到当前智能体，不能检索。"
        elif call.name == "read_more":
            if set(args) != {"source"}:
                return "read_more 只接受 source 参数。"
            source = args.get("source")
            if isinstance(source, bool) or not isinstance(source, int) or source < 1:
                return "source 必须是正整数来源编号。"
        elif call.name == "current_time":
            if args:
                return "current_time 不接受参数。"
        elif call.name == "use_mcp":
            if set(args) - {"server", "tool", "arguments"}:
                return "use_mcp 含有未声明的参数。"
            for key in ("server", "tool"):
                value = args.get(key)
                if not isinstance(value, str) or not value.strip() or len(value) > 80:
                    return f"{key} 必须是 1 到 80 个字符的字符串。"
            mcp_args = args.get("arguments", {})
            if not isinstance(mcp_args, dict):
                return "MCP arguments 必须是 JSON 对象。"
            if len(json.dumps(mcp_args, ensure_ascii=False, separators=(",", ":"))) > MAX_TOOL_ARGUMENT_CHARS:
                return "MCP arguments 过长，已拒绝执行。"
        return ""

    def _bounded_result(self, text: str) -> str:
        text = str(text or "")
        remaining = MAX_TOTAL_TOOL_RESULT_CHARS - self.result_chars
        if remaining <= 0:
            self.must_finalize = True
            return "工具结果总长度已达到上限，请根据现有资料作答。"
        limit = min(MAX_TOOL_RESULT_CHARS, remaining)
        if len(text) > limit:
            text = text[: max(0, limit - len(_RESULT_TRUNCATION))] + _RESULT_TRUNCATION
        self.result_chars += len(text)
        if self.result_chars >= MAX_TOTAL_TOOL_RESULT_CHARS:
            self.must_finalize = True
        return text

    async def _search(self, args: dict) -> tuple[str, dict]:
        query = str(args.get("query") or "").strip()
        if not query:
            return "query 不能为空。", {"error": True, "detail": "检索词为空"}
        query_key = re.sub(r"[^\w]+", "", unicodedata.normalize("NFKC", query).casefold())
        if query_key in self.search_queries:
            return "这个检索词已经执行过，请换用不同关键词或根据已有资料作答。", {
                "error": True,
                "detail": "重复检索已拦截",
            }
        if self.searches >= MAX_SEARCHES:
            return "知识库检索次数已达到上限，请根据已有资料作答。", {
                "error": True,
                "detail": "检索次数超限",
            }
        self.search_queries.add(query_key)
        self.searches += 1
        target = str(args.get("knowledge_base") or "").strip()
        ids = [kid for kid, name in self.kbs if name == target] if target else [kid for kid, _ in self.kbs]
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

    async def _mcp(self, args: dict) -> tuple[str, dict]:
        from youju.mcp_remote import call_tool

        server_name = str(args.get("server") or "").strip()
        tool = str(args.get("tool") or "").strip()
        raw_args = args.get("arguments", {})
        matching_servers = [s for s in self.mcp_servers if s.get("name") == server_name]
        if len(matching_servers) > 1:
            return f"MCP 服务名「{server_name}」不唯一，已拒绝调用。", {"error": True, "detail": "MCP 服务名不唯一"}
        server = matching_servers[0] if matching_servers else None
        if server is None:
            names = "、".join(s.get("name") or "" for s in self.mcp_servers) or "无"
            return f"没有名为「{server_name}」的已连接 MCP。当前有：{names}", {"error": True, "detail": "MCP 服务不存在"}
        public_tools = {
            str(item.get("name") or "") if isinstance(item, dict) else str(item)
            for item in server.get("tools") or []
        }
        if tool not in public_tools:
            return f"MCP 工具「{tool}」不在服务「{server_name}」公开的工具列表中，已拒绝调用。", {
                "error": True,
                "detail": "MCP 工具未公开",
            }
        if not server.get("url"):
            return "MCP 服务缺少有效地址，已拒绝调用。", {"error": True, "detail": "MCP 地址缺失"}
        try:
            text = await asyncio.to_thread(call_tool, server["url"], tool, raw_args)
        except Exception as exc:  # noqa: BLE001
            return f"调用 {server_name}/{tool} 失败：{exc}", {"error": True, "detail": str(exc)[:200]}
        return text, {"detail": f"{server_name} · {tool}"}
