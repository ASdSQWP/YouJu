"""共享的 HTTP 客户端。测试时把 transport_override 换成 httpx.MockTransport。"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx

TIMEOUT = httpx.Timeout(connect=15.0, read=120.0, write=30.0, pool=15.0)

transport_override = None


def async_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=TIMEOUT, transport=transport_override)


def sync_client() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT, transport=transport_override)


async def sse_data(response: httpx.Response) -> AsyncIterator[str]:
    async for line in response.aiter_lines():
        if line.startswith("data:"):
            yield line[5:].strip()
