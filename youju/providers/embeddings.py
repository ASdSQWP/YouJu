"""向量化。内置哈希向量保证离线可用，接入嵌入模型后走 /embeddings。"""

from __future__ import annotations

import hashlib
import time

import numpy as np

from youju.providers import http
from youju.providers.base import ProviderError, error_from_response
from youju.textproc import tokens

LOCAL_DIMS = 512


def normalize(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (matrix / norms).astype(np.float32)


def hash_embed(texts: list[str]) -> np.ndarray:
    matrix = np.zeros((len(texts), LOCAL_DIMS), dtype=np.float32)
    for row, text in enumerate(texts):
        for tok in tokens(text):
            digest = hashlib.blake2b(tok.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "little") % LOCAL_DIMS
            matrix[row, index] += 1.0 if digest[4] & 1 else -1.0
    return normalize(matrix)


class Embedder:
    def __init__(
        self,
        kind: str,
        model: str,
        base_url: str = "",
        api_key: str = "",
        extra_headers: dict | None = None,
        batch: int = 16,
        auth_header: str = "authorization",
    ):
        self.kind = kind
        self.model = model
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.extra_headers = dict(extra_headers or {})
        self.batch = max(1, int(batch or 16))
        self.auth_header = auth_header

    @property
    def local(self) -> bool:
        return self.kind == "local"

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            if self.kind == "gemini":
                headers["x-goog-api-key"] = self.api_key
            elif self.auth_header == "api-key":
                headers["api-key"] = self.api_key
            else:
                headers["Authorization"] = f"Bearer {self.api_key}"
        headers.update(self.extra_headers)
        return headers

    def embed(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, LOCAL_DIMS), dtype=np.float32)
        if self.local:
            return hash_embed(texts)
        rows: list[list[float]] = []
        with http.sync_client() as client:
            for start in range(0, len(texts), self.batch):
                rows.extend(self._batch(client, texts[start : start + self.batch]))
        return normalize(np.asarray(rows, dtype=np.float32))

    def _batch(self, client, batch: list[str]) -> list[list[float]]:
        for attempt in range(3):
            if self.kind == "gemini":
                model = self.model if self.model.startswith("models/") else f"models/{self.model}"
                resp = client.post(
                    f"{self.base_url}/{model}:batchEmbedContents",
                    json={"requests": [{"model": model, "content": {"parts": [{"text": t}]}} for t in batch]},
                    headers=self._headers(),
                )
            else:
                resp = client.post(
                    f"{self.base_url}/embeddings",
                    json={"model": self.model, "input": batch},
                    headers=self._headers(),
                )
            if resp.status_code == 429 and attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            if resp.status_code >= 400:
                raise error_from_response(resp.status_code, resp.content)
            data = resp.json()
            if self.kind == "gemini":
                vectors = [item.get("values") or [] for item in data.get("embeddings") or []]
            else:
                items = sorted(data.get("data") or [], key=lambda item: item.get("index", 0))
                vectors = [item.get("embedding") or [] for item in items]
            if len(vectors) != len(batch) or not all(vectors):
                raise ProviderError("向量接口返回的条数和请求不一致")
            return vectors
        raise ProviderError("向量接口持续限流，请稍后重试")
