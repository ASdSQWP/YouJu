"""接入（模拟的）大模型后的三种模式：检索增强、工具调用、模型故障降级。"""

import json
import re

import httpx
import pytest

from helpers import ask, sse_response
from youju.providers import http as provider_http


def first_source(text: str) -> tuple[int, str]:
    lines = text.splitlines()
    for i, line in enumerate(lines):
        match = re.match(r"^\[(\d+)\] 《", line)
        if match and i + 1 < len(lines):
            return int(match.group(1)), lines[i + 1].strip()
    return 1, "资料中没有相关依据。"


class ScriptedLLM:
    """模拟 OpenAI 兼容服务：有工具且还没检索时先要求检索，否则摘一句资料原文作答。"""

    def __init__(self):
        self.requests: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        tool_results = [m for m in body["messages"] if m["role"] == "tool"]
        if body.get("tools") and not tool_results and body.get("tool_choice") != "none":
            return sse_response(
                {"choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [{"index": 0, "id": "call_1", "type": "function", "function": {"name": "search_knowledge", "arguments": ""}}]}}]},
                {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"query": "CPU 使用率 排查步骤"}'}}]}, "finish_reason": "tool_calls"}]},
            )
        context = tool_results[-1]["content"] if tool_results else body["messages"][-1]["content"]
        number, sentence = first_source(context)
        answer = f"结论：{sentence.rstrip('。')}[{number}]。"
        return sse_response(
            {"choices": [{"index": 0, "delta": {"reasoning_content": "核对资料。"}}]},
            *[{"choices": [{"index": 0, "delta": {"content": answer[i : i + 6]}}]} for i in range(0, len(answer), 6)],
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 120, "completion_tokens": 30}},
        )


@pytest.fixture
def mock_provider(client):
    created = []

    def install(handler, tools=True):
        provider_http.transport_override = httpx.MockTransport(handler)
        row = client.post(
            "/api/providers",
            json={"preset": "custom", "name": "Mock", "base_url": "http://mock-llm/v1", "models": [{"id": "mock-chat", "type": "chat", "tools": tools}]},
        )
        assert row.status_code == 200, row.text
        created.append(row.json())
        return row.json()

    yield install
    for row in created:
        client.delete(f"/api/providers/{row['id']}")
    provider_http.transport_override = None


def test_provider_becomes_default_and_passes_connection_test(client, mock_provider):
    row = mock_provider(ScriptedLLM())
    assert client.post(f"/api/providers/{row['id']}/test", json={}).json()["ok"]
    models = client.get("/api/models").json()
    assert models["defaults"]["default_chat_model"] == f"{row['id']}:mock-chat"
    assert any(m["ref"] == f"{row['id']}:mock-chat" for m in models["chat"])
    listed = client.get("/api/providers").json()["providers"]
    assert listed[0]["has_key"] is False and "api_key" not in listed[0]


def test_rag_mode_grounded_answer(client, agents, mock_provider):
    llm = ScriptedLLM()
    mock_provider(llm)
    _, events, final = ask(client, agents["hr"]["id"], "去上海出差住宿标准是多少？", model=None)
    assert final["mode"] == "rag" and final["model"]["model"] == "mock-chat"
    assert final["reasoning"] == "核对资料。"
    assert final["usage"] == {"input": 120, "output": 30, "estimated": False}
    assert final["check"]["verdict"] == "pass", final["check"]
    assert len(final["used"]) == 1
    request = llm.requests[-1]
    assert "回答规则" in request["messages"][0]["content"] and "tools" not in request
    assert "参考资料" in request["messages"][-1]["content"]
    assert any(e["type"] == "reasoning" for e in events)


def test_agent_mode_calls_search_tool(client, agents, mock_provider):
    llm = ScriptedLLM()
    mock_provider(llm)
    _, events, final = ask(client, agents["itops"]["id"], "CPU 使用率一直很高怎么排查？", model=None)
    assert final["mode"] == "agent"
    tool_steps = [s for s in final["trace"] if s["kind"] == "tool"]
    assert tool_steps and tool_steps[0]["tool"] == "search_knowledge" and tool_steps[0]["status"] == "done"
    assert tool_steps[0]["arguments"] == {"query": "CPU 使用率 排查步骤"}
    assert final["check"]["verdict"] == "pass" and final["used"]
    assert llm.requests[0]["tools"][0]["function"]["name"] == "search_knowledge"
    assert llm.requests[1]["messages"][-1] == {"role": "tool", "tool_call_id": "call_1", "content": llm.requests[1]["messages"][-1]["content"]}
    assert [e["type"] for e in events].count("sources") >= 1


def test_model_without_tools_uses_rag(client, agents, mock_provider):
    llm = ScriptedLLM()
    mock_provider(llm, tools=False)
    _, _, final = ask(client, agents["itops"]["id"], "磁盘满了哪些文件可以删？", model=None)
    assert final["mode"] == "rag" and "tools" not in llm.requests[-1]


def test_model_outage_falls_back_to_offline(client, agents, mock_provider):
    mock_provider(lambda req: httpx.Response(503, json={"error": {"message": "overloaded"}}))
    _, _, final = ask(client, agents["safety"]["id"], "没有监护人可以动火吗？", model=None)
    assert final["mode"] == "fallback" and not final["error"]
    assert "禁止开始动火" in final["content"]
    assert any(s["kind"] == "fallback" for s in final["trace"])
