"""Anthropic Messages API（Claude）。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from youju.providers import http
from youju.providers.base import (
    ProviderError,
    StreamPart,
    ToolCall,
    ToolSpec,
    error_from_response,
    parse_arguments,
)

API_VERSION = "2023-06-01"


class AnthropicProvider:
    kind = "anthropic"

    def __init__(
        self,
        base_url: str = "https://api.anthropic.com",
        api_key: str = "",
        extra_headers: dict | None = None,
        max_tokens: int = 4096,
    ):
        base = (base_url or "https://api.anthropic.com").rstrip("/")
        self.base_url = base[:-3] if base.endswith("/v1") else base
        self.api_key = api_key or ""
        self.extra_headers = dict(extra_headers or {})
        self.max_tokens = max_tokens

    def headers(self) -> dict:
        headers = {"content-type": "application/json", "anthropic-version": API_VERSION}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        headers.update(self.extra_headers)
        return headers

    @staticmethod
    def wire(messages: list[dict]) -> tuple[str, list[dict]]:
        system: list[str] = []
        out: list[dict] = []

        def push(role: str, blocks: list[dict]) -> None:
            if not blocks:
                return
            if out and out[-1]["role"] == role:
                out[-1]["content"].extend(blocks)
            else:
                out.append({"role": role, "content": list(blocks)})

        for msg in messages:
            role = msg["role"]
            content = msg.get("content") or ""
            if role == "system":
                if content:
                    system.append(content)
            elif role == "user":
                if content:
                    push("user", [{"type": "text", "text": content}])
            elif role == "assistant":
                blocks: list[dict] = []
                if content.strip():
                    blocks.append({"type": "text", "text": content})
                for call in msg.get("tool_calls") or []:
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": call.id,
                            "name": call.name,
                            "input": call.arguments if isinstance(call.arguments, dict) else {},
                        }
                    )
                push("assistant", blocks)
            elif role == "tool":
                push(
                    "user",
                    [{"type": "tool_result", "tool_use_id": msg["tool_call_id"], "content": content or "（空）"}],
                )
        if out and out[0]["role"] != "user":
            out.insert(0, {"role": "user", "content": [{"type": "text", "text": "（继续）"}]})
        return "\n\n".join(system), out

    async def stream(
        self,
        model: str,
        messages: list[dict],
        tools: list[ToolSpec] | None = None,
        temperature: float | None = None,
        tool_choice: str | None = None,
    ) -> AsyncIterator[StreamPart]:
        system, wire = self.wire(messages)
        body: dict = {"model": model, "max_tokens": self.max_tokens, "messages": wire, "stream": True}
        if system:
            body["system"] = system
        if tools:
            body["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools
            ]
            if tool_choice == "none":
                body["tool_choice"] = {"type": "none"}
        if temperature is not None:
            body["temperature"] = temperature

        async with http.async_client() as client:
            async with client.stream(
                "POST", f"{self.base_url}/v1/messages", json=body, headers=self.headers()
            ) as resp:
                if resp.status_code >= 400:
                    raise error_from_response(resp.status_code, await resp.aread())
                blocks: dict[int, dict] = {}
                usage = {"input": 0, "output": 0}
                stop = ""
                async for data in http.sse_data(resp):
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    kind = obj.get("type")
                    if kind == "message_start":
                        raw = (obj.get("message") or {}).get("usage") or {}
                        usage["input"] = int(raw.get("input_tokens") or 0)
                        usage["output"] = int(raw.get("output_tokens") or 0)
                    elif kind == "content_block_start":
                        block = obj.get("content_block") or {}
                        blocks[obj.get("index", 0)] = {
                            "type": block.get("type"),
                            "id": block.get("id", ""),
                            "name": block.get("name", ""),
                            "json": "",
                        }
                        if block.get("type") == "text" and block.get("text"):
                            yield StreamPart("text", text=block["text"])
                    elif kind == "content_block_delta":
                        delta = obj.get("delta") or {}
                        dtype = delta.get("type")
                        if dtype == "text_delta" and delta.get("text"):
                            yield StreamPart("text", text=delta["text"])
                        elif dtype == "thinking_delta" and delta.get("thinking"):
                            yield StreamPart("reasoning", text=delta["thinking"])
                        elif dtype == "input_json_delta":
                            slot = blocks.setdefault(
                                obj.get("index", 0), {"type": "tool_use", "id": "", "name": "", "json": ""}
                            )
                            slot["json"] += delta.get("partial_json") or ""
                    elif kind == "message_delta":
                        stop = (obj.get("delta") or {}).get("stop_reason") or stop
                        raw = obj.get("usage") or {}
                        if raw.get("output_tokens") is not None:
                            usage["output"] = int(raw["output_tokens"])
                        if raw.get("input_tokens"):
                            usage["input"] = int(raw["input_tokens"])
                    elif kind == "error":
                        err = obj.get("error") or {}
                        raise ProviderError(f"模型返回错误：{err.get('message') or err}")
                calls = [
                    ToolCall(id=b["id"], name=b["name"], arguments=parse_arguments(b["json"]), raw_arguments=b["json"])
                    for _, b in sorted(blocks.items())
                    if b["type"] == "tool_use"
                ]
                yield StreamPart("usage", usage=usage)
                if calls:
                    yield StreamPart("tool_calls", calls=calls)
                yield StreamPart("end", stop=stop)

    async def list_models(self) -> list[str]:
        async with http.async_client() as client:
            resp = await client.get(f"{self.base_url}/v1/models?limit=100", headers=self.headers())
        if resp.status_code >= 400:
            raise error_from_response(resp.status_code, resp.content)
        return sorted({str(item["id"]) for item in resp.json().get("data") or [] if item.get("id")})
