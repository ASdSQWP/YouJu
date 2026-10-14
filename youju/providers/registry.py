"""把数据库里的模型服务配置变成可调用的客户端。

模型引用统一写成 "<服务ID>:<模型名>"，内置离线能力是 local:extractive 和 local:hash。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from youju.models import Provider, Setting
from youju.providers.anthropic import AnthropicProvider
from youju.providers.base import ProviderError
from youju.providers.embeddings import Embedder
from youju.providers.gemini import GeminiProvider
from youju.providers.openai_compat import OpenAICompatProvider
from youju.providers.presets import PRESET_MAP

LOCAL_CHAT = "local:extractive"
LOCAL_EMBED = "local:hash"


@dataclass
class ModelInfo:
    ref: str
    model: str
    type: str
    provider_id: int | None
    provider: str
    preset: str
    kind: str
    color: str
    tools: bool = True
    vision: bool = False
    extra: dict = field(default_factory=dict)

    @property
    def local(self) -> bool:
        return self.provider_id is None

    @property
    def label(self) -> str:
        if self.ref == LOCAL_CHAT:
            return "离线摘录"
        if self.ref == LOCAL_EMBED:
            return "内置哈希向量"
        return f"{self.provider} · {self.model}"

    def public(self) -> dict:
        return {
            "ref": self.ref,
            "model": self.model,
            "type": self.type,
            "provider": self.provider,
            "provider_id": self.provider_id,
            "preset": self.preset,
            "color": self.color,
            "tools": self.tools,
            "vision": self.vision,
            "local": self.local,
            "label": self.label,
        }


LOCAL_CHAT_INFO = ModelInfo(LOCAL_CHAT, "离线摘录", "chat", None, "内置", "local", "local", "#64748b", tools=False)
LOCAL_EMBED_INFO = ModelInfo(LOCAL_EMBED, "哈希向量", "embedding", None, "内置", "local", "local", "#64748b")


def get_setting(db: Session, key: str, default=None):
    row = db.get(Setting, key)
    return row.value if row is not None and row.value is not None else default


def set_setting(db: Session, key: str, value) -> None:
    row = db.get(Setting, key)
    if row is None:
        db.add(Setting(key=key, value=value))
    else:
        row.value = value


def models_of(row: Provider) -> list[ModelInfo]:
    color = (PRESET_MAP.get(row.preset) or {}).get("color") or "#64748b"
    out = []
    for item in row.models or []:
        model_id = str(item.get("id") or "").strip()
        if not model_id:
            continue
        out.append(
            ModelInfo(
                ref=f"{row.id}:{model_id}",
                model=model_id,
                type=item.get("type") or "chat",
                provider_id=row.id,
                provider=row.name,
                preset=row.preset,
                kind=row.kind,
                color=color,
                tools=bool(item.get("tools", True)),
                vision=bool(item.get("vision", False)),
                extra={k: item[k] for k in ("format", "url") if item.get(k)},
            )
        )
    return out


def list_models(db: Session, model_type: str | None = None) -> list[ModelInfo]:
    rows = db.scalars(select(Provider).where(Provider.enabled.is_(True)).order_by(Provider.id)).all()
    items = [info for row in rows for info in models_of(row)]
    if model_type:
        items = [info for info in items if info.type == model_type]
    if model_type in (None, "chat"):
        items.append(LOCAL_CHAT_INFO)
    if model_type in (None, "embedding"):
        items.append(LOCAL_EMBED_INFO)
    return items


def find_model(db: Session, ref: str | None) -> tuple[Provider | None, ModelInfo | None]:
    if not ref:
        return None, None
    if ref == LOCAL_CHAT:
        return None, LOCAL_CHAT_INFO
    if ref == LOCAL_EMBED:
        return None, LOCAL_EMBED_INFO
    provider_id, _, model_id = ref.partition(":")
    if not provider_id.isdigit() or not model_id:
        return None, None
    row = db.get(Provider, int(provider_id))
    if row is None or not row.enabled:
        return None, None
    for info in models_of(row):
        if info.model == model_id:
            return row, info
    return None, None


def chat_provider(row: Provider):
    options = row.options or {}
    if row.kind == "anthropic":
        return AnthropicProvider(row.base_url, row.api_key, row.extra_headers)
    if row.kind == "gemini":
        return GeminiProvider(row.base_url, row.api_key, row.extra_headers)
    return OpenAICompatProvider(
        row.base_url,
        row.api_key,
        row.extra_headers,
        stream_usage=bool(options.get("stream_usage")),
        auth_header=options.get("auth_header") or "authorization",
    )


def resolve_chat(db: Session, *refs: str | None):
    """按顺序找第一个可用的对话模型：用户临时选择 → 智能体配置 → 系统默认 → 任意已启用模型。"""
    for ref in [*refs, get_setting(db, "default_chat_model")]:
        if not ref:
            continue
        if ref == LOCAL_CHAT:
            return None, LOCAL_CHAT_INFO
        row, info = find_model(db, ref)
        if row is not None and info is not None and info.type == "chat":
            return chat_provider(row), info
    for info in list_models(db, "chat"):
        if not info.local:
            row, found = find_model(db, info.ref)
            if row is not None and found is not None:
                return chat_provider(row), found
    return None, LOCAL_CHAT_INFO


def embedder_from(row: Provider, info: ModelInfo) -> Embedder:
    options = row.options or {}
    return Embedder(
        "gemini" if row.kind == "gemini" else "openai",
        info.model,
        row.base_url,
        row.api_key,
        row.extra_headers,
        batch=int(options.get("embed_batch") or 16),
        auth_header=options.get("auth_header") or "authorization",
    )


def embedder_for(db: Session, ref: str | None) -> Embedder:
    if not ref or ref == LOCAL_EMBED:
        return Embedder("local", "hash")
    row, info = find_model(db, ref)
    if row is None or info is None or info.type != "embedding":
        raise ProviderError(f"向量模型 {ref} 不可用，请检查模型服务是否仍然启用")
    return embedder_from(row, info)


def rerank_config(row: Provider, info: ModelInfo) -> dict:
    return {
        "ref": info.ref,
        "model": info.model,
        "url": info.extra.get("url") or f"{row.base_url.rstrip('/')}/rerank",
        "format": info.extra.get("format") or "cohere",
        "api_key": row.api_key,
        "extra_headers": row.extra_headers or {},
    }


def reranker(db: Session) -> dict | None:
    row, info = find_model(db, get_setting(db, "rerank_model"))
    if row is None or info is None or info.type != "rerank":
        return None
    return rerank_config(row, info)
