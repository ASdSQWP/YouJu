import asyncio
from types import SimpleNamespace

import pytest

from youju.agent.sources import SourceBook
from youju.agent.tools import (
    MAX_SEARCHES,
    MAX_TOOL_CALLS,
    MAX_TOOL_RESULT_CHARS,
    MAX_TOTAL_TOOL_RESULT_CHARS,
    ToolBox,
)
from youju.providers.base import ToolCall


def run(box, name, arguments, raw_arguments=""):
    return asyncio.run(box.run(ToolCall("call_1", name, arguments, raw_arguments)))


def empty_retrieval():
    return SimpleNamespace(evidence=False, hits=[], reason="无命中", notes=[])


def test_repeated_search_and_search_budget_are_enforced(monkeypatch):
    queries = []

    def retrieve(_ids, query, _top_k, _strictness):
        queries.append(query)
        return empty_retrieval()

    monkeypatch.setattr("youju.agent.tools.retrieve", retrieve)
    box = ToolBox([(1, "运维手册")], SourceBook(), 5, "standard")
    first, _ = run(box, "search_knowledge", {"query": "CPU 使用率 排查步骤"})
    repeated, repeated_meta = run(box, "search_knowledge", {"query": "cpu使用率排查步骤"})
    assert "没有找到相关资料" in first
    assert "已经执行过" in repeated and repeated_meta["error"]
    assert len(queries) == 1

    for query in ("磁盘空间", "服务启动", "网络连接"):
        run(box, "search_knowledge", {"query": query})
    limited, meta = run(box, "search_knowledge", {"query": "新的关键词"})
    assert box.searches == MAX_SEARCHES
    assert len(queries) == MAX_SEARCHES
    assert "达到上限" in limited and meta["error"]


def test_invalid_tool_and_unbound_knowledge_base_are_rejected(monkeypatch):
    def unexpected(*_args, **_kwargs):
        pytest.fail("invalid tool request must not run retrieval")

    monkeypatch.setattr("youju.agent.tools.retrieve", unexpected)
    box = ToolBox([(1, "绑定知识库")], SourceBook(), 5, "standard")
    unknown, unknown_meta = run(box, "delete_everything", {})
    unbound, unbound_meta = run(
        box,
        "search_knowledge",
        {"query": "制度", "knowledge_base": "未绑定知识库"},
    )
    assert "未声明" in unknown and unknown_meta["error"]
    assert "未绑定" in unbound and unbound_meta["error"]


def test_oversized_arguments_and_results_are_bounded(monkeypatch):
    box = ToolBox([(1, "绑定知识库")], SourceBook(), 5, "standard")
    text, meta = run(
        box,
        "search_knowledge",
        {"query": "制度"},
        raw_arguments="x" * 4_001,
    )
    assert "过长" in text and meta["error"]
    invalid, invalid_meta = asyncio.run(box.run(ToolCall("call_bad", "current_time", {}, 123)))
    assert "参数格式无效" in invalid and invalid_meta["error"]
    capped = box._bounded_result("x" * (MAX_TOOL_RESULT_CHARS + 100))
    assert len(capped) <= MAX_TOOL_RESULT_CHARS and capped.endswith("[工具结果已截断]")


def test_total_tool_call_limit_is_enforced():
    box = ToolBox([], SourceBook(), 5, "standard")
    for _ in range(MAX_TOOL_CALLS):
        run(box, "current_time", {})
    text, meta = run(box, "current_time", {})
    assert box.tool_calls == MAX_TOOL_CALLS + 1
    assert "总次数已达到上限" in text and meta["error"]
    assert box.must_finalize


def test_total_result_limit_stops_subsequent_tool_execution(monkeypatch):
    from youju import mcp_remote

    calls = []
    monkeypatch.setattr(mcp_remote, "call_tool", lambda *args: calls.append(args) or "should not run")
    box = ToolBox(
        [],
        SourceBook(),
        5,
        "standard",
        mcp_servers=[{"name": "service", "url": "https://mcp.example", "tools": ["get_data"]}],
    )
    box.result_chars = MAX_TOTAL_TOOL_RESULT_CHARS
    result, meta = run(box, "use_mcp", {"server": "service", "tool": "get_data"})
    assert "结果总长度已达到上限" in result and meta["error"]
    assert box.must_finalize and calls == []


def test_mcp_server_and_tool_must_be_public(monkeypatch):
    from youju import mcp_remote

    calls = []
    monkeypatch.setattr(mcp_remote, "call_tool", lambda url, name, args: calls.append((url, name, args)) or "ok")
    box = ToolBox(
        [],
        SourceBook(),
        5,
        "standard",
        mcp_servers=[{"id": 8, "name": "工单服务", "url": "https://mcp.example", "tools": [{"name": "get_ticket"}]}],
    )
    denied, denied_meta = run(
        box,
        "use_mcp",
        {"server": "工单服务", "tool": "delete_ticket", "arguments": {"id": "1"}},
    )
    assert "公开的工具列表" in denied and denied_meta["error"]
    assert calls == []
    result, meta = run(
        box,
        "use_mcp",
        {"server": "工单服务", "tool": "get_ticket", "arguments": {"id": "1"}},
    )
    assert result == "ok" and not meta.get("error")
    assert calls == [("https://mcp.example", "get_ticket", {"id": "1"})]
