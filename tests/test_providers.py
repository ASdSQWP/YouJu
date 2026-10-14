import asyncio
import json

import httpx
import numpy as np
import pytest

from helpers import sse_response
from youju.providers import http as provider_http
from youju.providers.anthropic import AnthropicProvider
from youju.providers.base import ProviderError, ToolCall, ToolSpec
from youju.providers.embeddings import Embedder
from youju.providers.gemini import GeminiProvider
from youju.providers.openai_compat import OpenAICompatProvider
from youju.providers.rerank import rerank

SEARCH = ToolSpec("search_knowledge", "检索", {"type": "object", "properties": {"query": {"type": "string"}}})
NOW = ToolSpec("current_time", "时间", {"type": "object", "properties": {}})


@pytest.fixture
def transport():
    seen: list[httpx.Request] = []

    def install(handler):
        def wrapped(request):
            seen.append(request)
            return handler(request)

        provider_http.transport_override = httpx.MockTransport(wrapped)
        return seen

    yield install
    provider_http.transport_override = None


def drain(gen):
    async def go():
        return [part async for part in gen]

    return asyncio.run(go())


def test_openai_stream_text_reasoning_and_tool_call(transport):
    seen = transport(
        lambda req: sse_response(
            {"choices": [{"index": 0, "delta": {"reasoning_content": "想一想"}}]},
            {"choices": [{"index": 0, "delta": {"content": "好的"}}]},
            {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call_a", "function": {"name": "search_knowledge", "arguments": '{"query":'}}]}}]},
            {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": ' "安全带"}'}}]}, "finish_reason": "tool_calls"}]},
            {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 5}},
        )
    )
    provider = OpenAICompatProvider("https://api.example.com/v1", "sk-test", stream_usage=True)
    parts = drain(provider.stream("m", [{"role": "user", "content": "hi"}], tools=[SEARCH], temperature=0.2))
    assert [p.text for p in parts if p.kind == "reasoning"] == ["想一想"]
    assert [p.text for p in parts if p.kind == "text"] == ["好的"]
    call = next(p for p in parts if p.kind == "tool_calls").calls[0]
    assert (call.id, call.name, call.arguments) == ("call_a", "search_knowledge", {"query": "安全带"})
    assert next(p for p in parts if p.kind == "usage").usage == {"input": 12, "output": 5}
    body = json.loads(seen[0].content)
    assert body["stream_options"] == {"include_usage": True} and body["temperature"] == 0.2
    assert seen[0].headers["authorization"] == "Bearer sk-test"
    assert str(seen[0].url) == "https://api.example.com/v1/chat/completions"


def test_openai_drops_rejected_temperature(transport):
    bodies = []

    def handler(req):
        body = json.loads(req.content)
        bodies.append(body)
        if "temperature" in body:
            return httpx.Response(400, json={"error": {"message": "Unsupported value: 'temperature' does not support 0.3"}})
        return sse_response({"choices": [{"index": 0, "delta": {"content": "ok"}}]})

    transport(handler)
    parts = drain(OpenAICompatProvider("https://x/v1", "k").stream("gpt-5", [{"role": "user", "content": "hi"}], temperature=0.3))
    assert [p.text for p in parts if p.kind == "text"] == ["ok"]
    assert "temperature" in bodies[0] and "temperature" not in bodies[1]


def test_http_client_bypasses_loopback_proxy():
    client = provider_http.async_client()
    try:
        patterns = [getattr(key, "pattern", "") for key in client._mounts]
        assert "http://127.0.0.1" in patterns
        assert "http://localhost" in patterns
    finally:
        asyncio.run(client.aclose())


def test_openai_missing_model_lists_installed(transport):
    def handler(req):
        if str(req.url).endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "huihui_ai/qwen3-abliterated:8b"}]})
        return httpx.Response(404, json={"error": {"message": "model 'qwen3:8b' not found"}})

    transport(handler)
    with pytest.raises(ProviderError) as info:
        drain(OpenAICompatProvider("http://127.0.0.1:11434/v1").stream("qwen3:8b", [{"role": "user", "content": "hi"}]))
    assert "qwen3:8b" in str(info.value) and "huihui_ai/qwen3-abliterated:8b" in str(info.value)


def test_openai_error_message_and_azure_header(transport):
    seen = transport(lambda req: httpx.Response(401, json={"error": {"message": "Incorrect API key"}}))
    provider = OpenAICompatProvider("https://r.openai.azure.com/openai/v1", "az-key", auth_header="api-key")
    with pytest.raises(ProviderError) as info:
        drain(provider.stream("dep", [{"role": "user", "content": "hi"}]))
    assert info.value.status == 401 and "API Key" in str(info.value) and "Incorrect" in str(info.value)
    assert seen[0].headers["api-key"] == "az-key" and "authorization" not in seen[0].headers


def test_openai_wire_messages_replays_reasoning_in_tool_loop():
    call = ToolCall("call_1", "search_knowledge", {"query": "x"}, '{"query": "x"}')
    wire = OpenAICompatProvider.wire_messages(
        [
            {"role": "user", "content": "u"},
            {"role": "assistant", "content": "", "tool_calls": [call], "reasoning": "r"},
            {"role": "tool", "tool_call_id": "call_1", "content": "result"},
        ]
    )
    assert wire[1]["tool_calls"][0]["function"]["arguments"] == '{"query": "x"}'
    assert wire[1]["reasoning_content"] == "r"
    assert wire[2] == {"role": "tool", "tool_call_id": "call_1", "content": "result"}


def test_anthropic_stream_and_wire(transport):
    seen = transport(
        lambda req: sse_response(
            {"type": "message_start", "message": {"usage": {"input_tokens": 20, "output_tokens": 1}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "先检索"}},
            {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "toolu_1", "name": "search_knowledge", "input": {}}},
            {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"query": "挂'}},
            {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '钩"}'}},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 30}},
            {"type": "message_stop"},
            done=False,
        )
    )
    messages = [
        {"role": "system", "content": "规则"},
        {"role": "user", "content": "问"},
        {"role": "assistant", "content": "", "tool_calls": [ToolCall("toolu_0", "search_knowledge", {"query": "a"})]},
        {"role": "tool", "tool_call_id": "toolu_0", "content": "r1"},
        {"role": "user", "content": "继续"},
    ]
    parts = drain(AnthropicProvider("https://api.anthropic.com/v1", "sk-ant").stream("claude-sonnet-4-5", messages, tools=[SEARCH], tool_choice="none"))
    req = seen[0]
    body = json.loads(req.content)
    assert str(req.url) == "https://api.anthropic.com/v1/messages"
    assert req.headers["x-api-key"] == "sk-ant" and req.headers["anthropic-version"] == "2023-06-01"
    assert body["system"] == "规则" and body["max_tokens"] > 0 and body["tool_choice"] == {"type": "none"}
    assert body["messages"][1]["content"][0]["type"] == "tool_use"
    assert [b["type"] for b in body["messages"][2]["content"]] == ["tool_result", "text"]
    assert body["tools"][0]["input_schema"]["properties"]["query"]["type"] == "string"
    call = next(p for p in parts if p.kind == "tool_calls").calls[0]
    assert (call.id, call.arguments) == ("toolu_1", {"query": "挂钩"})
    assert next(p for p in parts if p.kind == "usage").usage == {"input": 20, "output": 30}
    assert "".join(p.text for p in parts if p.kind == "text") == "先检索"


def test_gemini_stream_keeps_signature_for_replay(transport):
    seen = transport(
        lambda req: sse_response(
            {"candidates": [{"content": {"role": "model", "parts": [{"text": "思考中", "thought": True}]}}]},
            {
                "candidates": [
                    {
                        "content": {"role": "model", "parts": [{"functionCall": {"name": "search_knowledge", "args": {"query": "烟火"}}, "thoughtSignature": "sig"}]},
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {"promptTokenCount": 9, "candidatesTokenCount": 4, "thoughtsTokenCount": 2},
            },
            done=False,
        )
    )
    messages = [{"role": "system", "content": "规则"}, {"role": "user", "content": "问"}]
    parts = drain(GeminiProvider(api_key="g-key").stream("gemini-2.5-flash", messages, tools=[SEARCH, NOW]))
    req = seen[0]
    body = json.loads(req.content)
    assert str(req.url).endswith("/models/gemini-2.5-flash:streamGenerateContent?alt=sse")
    assert req.headers["x-goog-api-key"] == "g-key"
    assert body["systemInstruction"]["parts"][0]["text"] == "规则"
    declarations = body["tools"][0]["functionDeclarations"]
    assert "parameters" in declarations[0] and "parameters" not in declarations[1]
    assert [p.text for p in parts if p.kind == "reasoning"] == ["思考中"]
    tool_part = next(p for p in parts if p.kind == "tool_calls")
    assert tool_part.calls[0].arguments == {"query": "烟火"}
    assert next(p for p in parts if p.kind == "usage").usage == {"input": 9, "output": 6}
    _, contents = GeminiProvider.wire(
        [
            {"role": "user", "content": "问"},
            {"role": "assistant", "content": "", "tool_calls": tool_part.calls, "raw": tool_part.raw},
            {"role": "tool", "tool_call_id": tool_part.calls[0].id, "name": "search_knowledge", "content": "结果"},
        ]
    )
    assert contents[1]["role"] == "model" and contents[1]["parts"][-1]["thoughtSignature"] == "sig"
    assert contents[2]["parts"][0]["functionResponse"] == {"name": "search_knowledge", "response": {"result": "结果"}}


def test_openai_embeddings_batch_and_order(transport):
    batches = []

    def handler(req):
        texts = json.loads(req.content)["input"]
        batches.append(texts)
        data = [{"index": i, "embedding": [float(len(t)), 1.0, 0.0]} for i, t in enumerate(texts)]
        return httpx.Response(200, json={"data": list(reversed(data))})

    transport(handler)
    vectors = Embedder("openai", "m", "https://x/v1", "k", batch=2).embed(["a", "bb", "ccc"])
    assert batches == [["a", "bb"], ["ccc"]]
    assert vectors.shape == (3, 3)
    assert np.allclose(np.linalg.norm(vectors, axis=1), 1.0)
    assert vectors[1][0] > vectors[0][0]


def test_gemini_embeddings(transport):
    seen = transport(lambda req: httpx.Response(200, json={"embeddings": [{"values": [1.0, 0.0]}, {"values": [0.0, 2.0]}]}))
    vectors = Embedder("gemini", "gemini-embedding-001", "https://g/v1beta", "k").embed(["a", "b"])
    assert str(seen[0].url) == "https://g/v1beta/models/gemini-embedding-001:batchEmbedContents"
    assert np.allclose(vectors, [[1.0, 0.0], [0.0, 1.0]])


def test_rerank_cohere_and_dashscope(transport):
    seen = transport(
        lambda req: httpx.Response(
            200,
            json={"output": {"results": [{"index": 1, "relevance_score": 0.9}]}}
            if "dashscope" in str(req.url)
            else {"results": [{"index": 0, "relevance_score": 0.8}]},
        )
    )
    assert rerank({"url": "https://s/v1/rerank", "model": "bge", "api_key": "k"}, "q", ["a", "b"], 2) == [(0, 0.8)]
    assert rerank({"url": "https://dashscope.x/rerank", "model": "gte", "api_key": "k", "format": "dashscope"}, "q", ["a", "b"], 2) == [(1, 0.9)]
    assert json.loads(seen[1].content)["input"] == {"query": "q", "documents": ["a", "b"]}
