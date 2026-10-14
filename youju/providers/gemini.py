"""Google Gemini generateContent 原生协议。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from youju.providers import http
from youju.providers.base import ProviderError, StreamPart, ToolCall, ToolSpec, error_from_response

SYNTHETIC = "gemini_call_"


class GeminiProvider:
    kind = "gemini"

    def __init__(
        self,
        base_url: str = "https://generativelanguage.googleapis.com/v1beta",
        api_key: str = "",
        extra_headers: dict | None = None,
    ):
        self.base_url = (base_url or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
        self.api_key = api_key or ""
        self.extra_headers = dict(extra_headers or {})

    def headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["x-goog-api-key"] = self.api_key
        headers.update(self.extra_headers)
        return headers

    @staticmethod
    def wire(messages: list[dict]) -> tuple[str, list[dict]]:
        system: list[str] = []
        contents: list[dict] = []

        def push(role: str, parts: list[dict]) -> None:
            if not parts:
                return
            if contents and contents[-1]["role"] == role:
                contents[-1]["parts"].extend(parts)
            else:
                contents.append({"role": role, "parts": list(parts)})

        for msg in messages:
            role = msg["role"]
            content = msg.get("content") or ""
            if role == "system":
                if content:
                    system.append(content)
            elif role == "user":
                if content:
                    push("user", [{"text": content}])
            elif role == "assistant":
                raw = msg.get("raw")
                if isinstance(raw, list) and raw:
                    # 原样回放，保留 thoughtSignature
                    parts = [dict(part) for part in raw]
                else:
                    parts = [{"text": content}] if content else []
                    for call in msg.get("tool_calls") or []:
                        fc: dict = {"name": call.name, "args": call.arguments}
                        if not call.id.startswith(SYNTHETIC):
                            fc["id"] = call.id
                        parts.append({"functionCall": fc})
                push("model", parts)
            elif role == "tool":
                response: dict = {"name": msg.get("name") or "tool", "response": {"result": content}}
                call_id = msg.get("tool_call_id") or ""
                if call_id and not call_id.startswith(SYNTHETIC):
                    response["id"] = call_id
                push("user", [{"functionResponse": response}])
        return "\n\n".join(system), contents

    async def stream(
        self,
        model: str,
        messages: list[dict],
        tools: list[ToolSpec] | None = None,
        temperature: float | None = None,
        tool_choice: str | None = None,
    ) -> AsyncIterator[StreamPart]:
        system, contents = self.wire(messages)
        body: dict = {"contents": contents}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        if tools:
            declarations = []
            for t in tools:
                decl: dict = {"name": t.name, "description": t.description}
                if (t.parameters or {}).get("properties"):
                    decl["parameters"] = t.parameters
                declarations.append(decl)
            body["tools"] = [{"functionDeclarations": declarations}]
            if tool_choice == "none":
                body["toolConfig"] = {"functionCallingConfig": {"mode": "NONE"}}
        if temperature is not None:
            body["generationConfig"] = {"temperature": temperature}
        path = model if model.startswith("models/") else f"models/{model}"
        url = f"{self.base_url}/{path}:streamGenerateContent?alt=sse"

        async with http.async_client() as client:
            async with client.stream("POST", url, json=body, headers=self.headers()) as resp:
                if resp.status_code >= 400:
                    raise error_from_response(resp.status_code, await resp.aread())
                raw_parts: list[dict] = []
                calls: list[ToolCall] = []
                usage = None
                stop = ""
                async for data in http.sse_data(resp):
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("error"):
                        err = obj["error"]
                        raise ProviderError(f"模型返回错误：{err.get('message') if isinstance(err, dict) else err}")
                    blocked = (obj.get("promptFeedback") or {}).get("blockReason")
                    if blocked:
                        raise ProviderError(f"内容被模型安全策略拦截：{blocked}")
                    candidates = obj.get("candidates") or []
                    if candidates:
                        cand = candidates[0]
                        for part in (cand.get("content") or {}).get("parts") or []:
                            raw_parts.append(part)
                            if "functionCall" in part:
                                fc = part["functionCall"] or {}
                                args = fc.get("args") or {}
                                calls.append(
                                    ToolCall(
                                        id=fc.get("id") or f"{SYNTHETIC}{len(calls)}",
                                        name=fc.get("name") or "",
                                        arguments=args if isinstance(args, dict) else {},
                                        raw_arguments=json.dumps(args, ensure_ascii=False),
                                    )
                                )
                            elif part.get("text"):
                                kind = "reasoning" if part.get("thought") else "text"
                                yield StreamPart(kind, text=part["text"])
                        if cand.get("finishReason"):
                            stop = cand["finishReason"]
                    meta = obj.get("usageMetadata")
                    if meta:
                        usage = {
                            "input": int(meta.get("promptTokenCount") or 0),
                            "output": int(meta.get("candidatesTokenCount") or 0)
                            + int(meta.get("thoughtsTokenCount") or 0),
                        }
                compact = _compact(raw_parts)
                if usage:
                    yield StreamPart("usage", usage=usage)
                if calls:
                    yield StreamPart("tool_calls", calls=calls, raw=compact)
                yield StreamPart("end", stop=stop, raw=compact)

    async def list_models(self) -> list[str]:
        async with http.async_client() as client:
            resp = await client.get(f"{self.base_url}/models?pageSize=200", headers=self.headers())
        if resp.status_code >= 400:
            raise error_from_response(resp.status_code, resp.content)
        names = []
        for item in resp.json().get("models") or []:
            name = str(item.get("name") or "")
            methods = item.get("supportedGenerationMethods") or []
            if name and ("generateContent" in methods or "embedContent" in methods):
                names.append(name.removeprefix("models/"))
        return sorted(set(names))


def _compact(parts: list[dict]) -> list[dict]:
    """合并相邻的纯文本片段，带签名或函数调用的片段原样保留。"""
    out: list[dict] = []
    for part in parts:
        if set(part) == {"text"} and out and set(out[-1]) == {"text"}:
            out[-1] = {"text": out[-1]["text"] + part["text"]}
        else:
            out.append(dict(part))
    return out
