"""重排接口。cohere 格式覆盖硅基流动、Jina、Cohere；dashscope 格式对应阿里云 gte-rerank。"""

from __future__ import annotations

from youju.providers import http
from youju.providers.base import error_from_response


def rerank(cfg: dict, query: str, documents: list[str], top_n: int) -> list[tuple[int, float]]:
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {cfg.get('api_key') or ''}"}
    headers.update(cfg.get("extra_headers") or {})
    if cfg.get("format") == "dashscope":
        body = {
            "model": cfg["model"],
            "input": {"query": query, "documents": documents},
            "parameters": {"top_n": top_n, "return_documents": False},
        }
    else:
        body = {"model": cfg["model"], "query": query, "documents": documents, "top_n": top_n}
    with http.sync_client() as client:
        resp = client.post(cfg["url"], json=body, headers=headers)
    if resp.status_code >= 400:
        raise error_from_response(resp.status_code, resp.content)
    data = resp.json()
    if cfg.get("format") == "dashscope":
        results = (data.get("output") or {}).get("results") or []
    else:
        results = data.get("results") or []
    return [
        (int(item["index"]), float(item.get("relevance_score", item.get("score", 0.0))))
        for item in results
        if "index" in item
    ]
