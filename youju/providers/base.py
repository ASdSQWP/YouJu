"""各家协议统一成同一种流式输出，编排层不关心底下是哪家模型。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict
    raw_arguments: str = ""


@dataclass
class StreamPart:
    """kind 取值：text、reasoning、tool_calls、usage、end。"""

    kind: str
    text: str = ""
    calls: list[ToolCall] = field(default_factory=list)
    usage: dict | None = None
    raw: Any = None
    stop: str = ""


class ProviderError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class ChatProvider(Protocol):
    def stream(
        self,
        model: str,
        messages: list[dict],
        tools: list[ToolSpec] | None = None,
        temperature: float | None = None,
        tool_choice: str | None = None,
    ) -> AsyncIterator[StreamPart]: ...

    async def list_models(self) -> list[str]: ...


def parse_arguments(raw: str) -> dict:
    if not raw or not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {"_invalid": raw[:500]}
    return value if isinstance(value, dict) else {"value": value}


_HINTS = {
    400: "请求参数不被接受",
    401: "API Key 无效或已过期",
    403: "没有权限访问该模型",
    404: "接口地址或模型名称不正确",
    408: "请求超时",
    413: "请求内容过长",
    429: "请求过于频繁或账户额度不足",
}


def error_from_response(status: int, body: bytes | str) -> ProviderError:
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body)
    detail = text.strip()
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        obj = None
    if isinstance(obj, dict):
        err = obj.get("error")
        if isinstance(err, dict):
            detail = str(err.get("message") or err.get("code") or detail)
        elif isinstance(err, str):
            detail = err
        elif obj.get("message"):
            detail = str(obj["message"])
    hint = _HINTS.get(status) or ("模型服务暂时不可用" if status >= 500 else "请求被拒绝")
    return ProviderError(f"{hint}（HTTP {status}）：{detail[:300]}", status=status)
