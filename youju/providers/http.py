"""共享的 HTTP 客户端。测试时把 transport_override 换成 httpx.MockTransport。"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx

TIMEOUT = httpx.Timeout(connect=15.0, read=120.0, write=30.0, pool=15.0)

transport_override = None

# 系统代理经常把 127.0.0.1 转成 502，本地 Ollama / vLLM 会表现为「模型没接上」
_LOOPBACK = ("http://127.0.0.1", "https://127.0.0.1", "http://localhost", "https://localhost", "http://[::1]", "https://[::1]")


def _mounts(async_mode: bool) -> dict:
    cls = httpx.AsyncHTTPTransport if async_mode else httpx.HTTPTransport
    return {origin: cls(proxy=None, trust_env=False) for origin in _LOOPBACK}


def async_client(timeout: httpx.Timeout | None = None) -> httpx.AsyncClient:
    t = timeout or TIMEOUT
    if transport_override is not None:
        return httpx.AsyncClient(timeout=t, transport=transport_override)
    return httpx.AsyncClient(timeout=t, mounts=_mounts(True))


def sync_client(timeout: httpx.Timeout | None = None) -> httpx.Client:
    t = timeout or TIMEOUT
    if transport_override is not None:
        return httpx.Client(timeout=t, transport=transport_override)
    return httpx.Client(timeout=t, mounts=_mounts(False))


async def sse_data(response: httpx.Response) -> AsyncIterator[str]:
    async for line in response.aiter_lines():
        if line.startswith("data:"):
            yield line[5:].strip()
