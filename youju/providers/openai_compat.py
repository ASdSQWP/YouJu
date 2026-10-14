"""OpenAI Chat Completions 兼容协议。

DeepSeek、通义千问、智谱、Kimi、豆包、文心、混元、MiniMax、硅基流动、Ollama 等都走这里。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx

from youju.providers import http
from youju.providers.base import (
    ProviderError,
    StreamPart,
    ToolCall,
    ToolSpec,
    error_from_response,
    parse_arguments,
)


class OpenAICompatProvider:
    kind = "openai"

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        extra_headers: dict | None = None,
        stream_usage: bool = False,
        auth_header: str = "authorization",
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or ""
        self.extra_headers = dict(extra_headers or {})
        self.stream_usage = stream_usage
        self.auth_header = auth_header

    def headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            if self.auth_header == "api-key":
                headers["api-key"] = self.api_key
            else:
                headers["Authorization"] = f"Bearer {self.api_key}"
        headers.update(self.extra_headers)
        return headers

    @staticmethod
    def wire_messages(messages: list[dict]) -> list[dict]:
        out: list[dict] = []
        for msg in messages:
            role = msg["role"]
            if role == "assistant":
                item: dict = {"role": "assistant", "content": msg.get("content") or ""}
                calls = msg.get("tool_calls") or []
                if calls:
                    item["tool_calls"] = [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": call.raw_arguments
                                or json.dumps(call.arguments, ensure_ascii=False),
                            },
                        }
                        for call in calls
                    ]
                    # 思考型模型在同一轮工具调用里需要拿回自己的推理内容
                    if msg.get("reasoning"):
                        item["reasoning_content"] = msg["reasoning"]
                out.append(item)
            elif role == "tool":
                out.append(
                    {"role": "tool", "tool_call_id": msg["tool_call_id"], "content": msg.get("content") or ""}
                )
            else:
                out.append({"role": role, "content": msg.get("content") or ""})
        return out

    async def stream(
        self,
        model: str,
        messages: list[dict],
        tools: list[ToolSpec] | None = None,
        temperature: float | None = None,
        tool_choice: str | None = None,
    ) -> AsyncIterator[StreamPart]:
        body: dict = {"model": model, "messages": self.wire_messages(messages), "stream": True}
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {"name": t.name, "description": t.description, "parameters": t.parameters},
                }
                for t in tools
            ]
            if tool_choice:
                body["tool_choice"] = tool_choice
        if temperature is not None:
            body["temperature"] = temperature
        if self.stream_usage:
            body["stream_options"] = {"include_usage": True}

        url = f"{self.base_url}/chat/completions"
        try:
            async with http.async_client() as client:
                for attempt in range(3):
                    async with client.stream("POST", url, json=body, headers=self.headers()) as resp:
                        if resp.status_code >= 400:
                            raw = await resp.aread()
                            if attempt < 2 and resp.status_code == 400 and _drop_rejected(body, raw):
                                continue
                            if resp.status_code in (404, 400) and _looks_missing_model(raw, model):
                                raise await self._missing_model(model, resp.status_code, raw)
                            if resp.status_code == 502 and _is_loopback(self.base_url):
                                raise ProviderError(
                                    f"访问 {self.base_url} 返回 502，通常是系统代理劫持了本机地址。"
                                    "请重启有据后再试，或把 127.0.0.1 加入代理旁路。",
                                    status=502,
                                )
                            raise error_from_response(resp.status_code, raw)
                        ctype = resp.headers.get("content-type", "")
                        if "event-stream" not in ctype and "json" in ctype:
                            payload = json.loads(await resp.aread())
                            for part in _from_completion(payload):
                                yield part
                            return
                        async for part in _from_stream(resp):
                            yield part
                        return
        except ProviderError:
            raise
        except httpx.ConnectError as exc:
            raise ProviderError(
                f"连不上 {self.base_url}。本地 Ollama 需要先启动（命令行执行 ollama serve），"
                "并确认设置里的模型名和 `ollama list` 完全一致。",
                status=None,
            ) from exc
        except httpx.ProxyError as exc:
            raise ProviderError(
                f"请求被系统代理拦截，无法访问 {self.base_url}。本地模型请把 127.0.0.1 加入代理旁路。",
                status=502,
            ) from exc
        except httpx.TimeoutException as exc:
            raise ProviderError(f"调用 {model} 超时，模型可能正在加载，请稍后重试。") from exc

    async def list_models(self) -> list[str]:
        try:
            async with http.async_client() as client:
                resp = await client.get(f"{self.base_url}/models", headers=self.headers())
        except httpx.ConnectError as exc:
            raise ProviderError(f"连不上 {self.base_url}，请确认 Ollama 已启动。") from exc
        except httpx.ProxyError as exc:
            raise ProviderError(f"系统代理拦截了 {self.base_url}。") from exc
        if resp.status_code >= 400:
            raise error_from_response(resp.status_code, resp.content)
        data = resp.json()
        items = data.get("data") if isinstance(data, dict) else data
        return sorted({str(item["id"]) for item in items or [] if isinstance(item, dict) and item.get("id")})

    async def _missing_model(self, model: str, status: int, raw: bytes) -> ProviderError:
        installed = []
        try:
            installed = await self.list_models()
        except Exception:  # noqa: BLE001
            pass
        if installed:
            shown = "、".join(installed[:8])
            extra = f" 等 {len(installed)} 个" if len(installed) > 8 else ""
            return ProviderError(
                f"Ollama 里没有「{model}」。当前已安装：{shown}{extra}。"
                "请打开「设置 → 模型服务」，点「从接口获取」勾选实际模型名。",
                status=status,
            )
        return error_from_response(status, raw)


def _is_loopback(url: str) -> bool:
    host = (url or "").lower()
    return any(token in host for token in ("127.0.0.1", "localhost", "[::1]", "0.0.0.0"))


def _looks_missing_model(raw: bytes, model: str) -> bool:
    text = raw.decode("utf-8", "replace").lower()
    name = (model or "").lower()
    return "not found" in text or "does not exist" in text or (name and name in text and "model" in text)


def _drop_rejected(body: dict, raw: bytes) -> bool:
    """推理模型不接受 temperature，部分兼容实现不认 stream_options，去掉后重试一次。"""
    text = raw.decode("utf-8", "replace").lower()
    for key in ("temperature", "stream_options", "tool_choice"):
        if key in text and key in body:
            body.pop(key)
            return True
    return False


def _usage(raw: dict) -> dict:
    return {
        "input": int(raw.get("prompt_tokens") or raw.get("input_tokens") or 0),
        "output": int(raw.get("completion_tokens") or raw.get("output_tokens") or 0),
    }


async def _from_stream(resp) -> AsyncIterator[StreamPart]:
    calls: dict[int, dict] = {}
    finish = ""
    async for data in http.sse_data(resp):
        if data == "[DONE]":
            break
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        err = obj.get("error")
        if err:
            message = err.get("message") if isinstance(err, dict) else str(err)
            raise ProviderError(f"模型返回错误：{message}")
        if obj.get("usage"):
            yield StreamPart("usage", usage=_usage(obj["usage"]))
        for choice in obj.get("choices") or []:
            delta = choice.get("delta") or {}
            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
            if isinstance(reasoning, str) and reasoning:
                yield StreamPart("reasoning", text=reasoning)
            content = delta.get("content")
            if isinstance(content, str) and content:
                yield StreamPart("text", text=content)
            for tc in delta.get("tool_calls") or []:
                index = tc.get("index")
                if index is None:
                    index = len(calls) if (tc.get("id") or not calls) else max(calls)
                slot = calls.setdefault(index, {"id": "", "name": "", "args": ""})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name") and not slot["name"]:
                    slot["name"] = fn["name"]
                piece = fn.get("arguments")
                if piece:
                    slot["args"] += piece if isinstance(piece, str) else json.dumps(piece, ensure_ascii=False)
            if choice.get("finish_reason"):
                finish = choice["finish_reason"]
    if calls:
        yield StreamPart("tool_calls", calls=[_call(index, slot) for index, slot in sorted(calls.items())])
    yield StreamPart("end", stop=finish)


def _call(index: int, slot: dict) -> ToolCall:
    return ToolCall(
        id=slot["id"] or f"call_{index}",
        name=slot["name"],
        arguments=parse_arguments(slot["args"]),
        raw_arguments=slot["args"],
    )


def _from_completion(payload: dict) -> list[StreamPart]:
    parts: list[StreamPart] = []
    choice = (payload.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    if message.get("reasoning_content"):
        parts.append(StreamPart("reasoning", text=message["reasoning_content"]))
    if message.get("content"):
        parts.append(StreamPart("text", text=message["content"]))
    calls = []
    for index, tc in enumerate(message.get("tool_calls") or []):
        fn = tc.get("function") or {}
        args = fn.get("arguments") or ""
        if not isinstance(args, str):
            args = json.dumps(args, ensure_ascii=False)
        calls.append(_call(index, {"id": tc.get("id") or "", "name": fn.get("name") or "", "args": args}))
    if payload.get("usage"):
        parts.append(StreamPart("usage", usage=_usage(payload["usage"])))
    if calls:
        parts.append(StreamPart("tool_calls", calls=calls))
    parts.append(StreamPart("end", stop=choice.get("finish_reason") or ""))
    return parts
