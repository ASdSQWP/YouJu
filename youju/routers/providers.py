"""模型服务配置。密钥只有管理员能改，接口只返回打码后的值。"""

from __future__ import annotations

import asyncio
import time
from contextlib import aclosing
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from youju.db import get_db, iso
from youju.deps import admin_user, current_user
from youju.models import Agent, KnowledgeBase, Provider, User
from youju.providers.base import ProviderError
from youju.providers.presets import PRESET_MAP, PRESETS
from youju.providers.registry import (
    LOCAL_CHAT,
    LOCAL_EMBED,
    chat_provider,
    embedder_from,
    find_model,
    get_setting,
    list_models,
    models_of,
    rerank_config,
    set_setting,
)
from youju.providers.rerank import rerank
from youju.security import mask_secret

router = APIRouter(tags=["models"])


class ModelItem(BaseModel):
    id: str = Field(min_length=1, max_length=160)
    type: Literal["chat", "embedding", "rerank"] = "chat"
    tools: bool = True
    vision: bool = False
    format: str | None = Field(default=None, max_length=16)
    url: str | None = Field(default=None, max_length=255)


class ProviderBody(BaseModel):
    preset: str = Field(max_length=32)
    name: str | None = Field(default=None, max_length=64)
    base_url: str | None = Field(default=None, max_length=255)
    api_key: str | None = Field(default=None, max_length=1000)
    models: list[ModelItem] | None = None
    extra_headers: dict[str, str] | None = None
    enabled: bool = True


class ProviderPatch(BaseModel):
    name: str | None = Field(default=None, max_length=64)
    base_url: str | None = Field(default=None, max_length=255)
    api_key: str | None = Field(default=None, max_length=1000)
    models: list[ModelItem] | None = None
    extra_headers: dict[str, str] | None = None
    enabled: bool | None = None


class TestBody(BaseModel):
    model: str | None = None


class DefaultsBody(BaseModel):
    default_chat_model: str | None = None
    default_embedding_model: str | None = None
    rerank_model: str | None = None


def _check_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if not url.startswith(("http://", "https://")) or "<" in url:
        raise HTTPException(status_code=400, detail="接口地址需要以 http:// 或 https:// 开头，并替换掉尖括号里的占位内容")
    return url


def _clean_models(items: list[ModelItem]) -> list[dict]:
    seen: set[tuple[str, str]] = set()
    out = []
    for item in items:
        key = (item.id.strip(), item.type)
        if key in seen:
            continue
        seen.add(key)
        data: dict = {"id": key[0], "type": item.type}
        if item.type == "chat":
            data["tools"] = item.tools
            data["vision"] = item.vision
        if item.type == "rerank":
            data["format"] = item.format or "cohere"
            if item.url:
                data["url"] = item.url.strip()
        out.append(data)
    return out


def _merge_headers(new: dict[str, str], old: dict | None) -> dict:
    merged = {}
    for key, value in new.items():
        merged[key] = (old or {}).get(key, "") if "•" in value else value
    return merged


def provider_public(row: Provider) -> dict:
    preset = PRESET_MAP.get(row.preset) or {}
    return {
        "id": row.id,
        "preset": row.preset,
        "name": row.name,
        "kind": row.kind,
        "base_url": row.base_url,
        "has_key": bool(row.api_key),
        "api_key_masked": mask_secret(row.api_key),
        "extra_headers": {k: mask_secret(v) for k, v in (row.extra_headers or {}).items()},
        "models": row.models or [],
        "enabled": row.enabled,
        "color": preset.get("color", "#64748b"),
        "region": preset.get("region", "local"),
        "key_url": preset.get("key_url", ""),
        "note": preset.get("note", ""),
        "updated_at": iso(row.updated_at),
    }


def _defaults(db: Session) -> dict:
    return {
        "default_chat_model": get_setting(db, "default_chat_model"),
        "default_embedding_model": get_setting(db, "default_embedding_model") or LOCAL_EMBED,
        "rerank_model": get_setting(db, "rerank_model"),
    }


def _get(db: Session, provider_id: int) -> Provider:
    row = db.get(Provider, provider_id)
    if row is None:
        raise HTTPException(status_code=404, detail="模型服务不存在")
    return row


@router.get("/api/providers/presets")
def presets(_admin: User = Depends(admin_user)):
    return {"presets": PRESETS}


@router.get("/api/providers")
def list_providers(_admin: User = Depends(admin_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(Provider).order_by(Provider.id)).all()
    return {"providers": [provider_public(row) for row in rows], "defaults": _defaults(db)}


@router.post("/api/providers")
def create_provider(body: ProviderBody, _admin: User = Depends(admin_user), db: Session = Depends(get_db)):
    preset = PRESET_MAP.get(body.preset)
    if preset is None:
        raise HTTPException(status_code=400, detail="未知的模型服务类型")
    api_key = (body.api_key or "").strip()
    if preset.get("needs_key", True) and not api_key:
        raise HTTPException(status_code=400, detail="请填写 API Key")
    row = Provider(
        preset=body.preset,
        name=(body.name or preset["name"]).strip() or preset["name"],
        kind=preset["kind"],
        base_url=_check_url(body.base_url or preset["base_url"]),
        api_key=api_key,
        extra_headers=dict(body.extra_headers or {}),
        models=_clean_models(body.models) if body.models is not None else [dict(m) for m in preset["models"]],
        options={
            "stream_usage": bool(preset.get("stream_usage")),
            "embed_batch": int(preset.get("embed_batch", 16)),
            "auth_header": preset.get("auth_header", "authorization"),
        },
        enabled=body.enabled,
    )
    db.add(row)
    db.commit()
    if not get_setting(db, "default_chat_model"):
        first = next((info for info in models_of(row) if info.type == "chat"), None)
        if first is not None and row.enabled:
            set_setting(db, "default_chat_model", first.ref)
            db.commit()
    return provider_public(row)


@router.patch("/api/providers/{provider_id}")
def update_provider(
    provider_id: int, body: ProviderPatch, _admin: User = Depends(admin_user), db: Session = Depends(get_db)
):
    row = _get(db, provider_id)
    if body.name is not None and body.name.strip():
        row.name = body.name.strip()
    if body.base_url is not None:
        row.base_url = _check_url(body.base_url)
    if body.api_key is not None and "•" not in body.api_key:
        row.api_key = body.api_key.strip()
    if body.models is not None:
        row.models = _clean_models(body.models)
    if body.extra_headers is not None:
        row.extra_headers = _merge_headers(body.extra_headers, row.extra_headers)
    if body.enabled is not None:
        row.enabled = body.enabled
    db.commit()
    return provider_public(row)


@router.delete("/api/providers/{provider_id}")
def delete_provider(provider_id: int, _admin: User = Depends(admin_user), db: Session = Depends(get_db)):
    row = _get(db, provider_id)
    prefix = f"{row.id}:"
    using = db.scalars(select(KnowledgeBase.name).where(KnowledgeBase.embedding_ref.like(prefix + "%"))).all()
    if using:
        raise HTTPException(status_code=400, detail=f"知识库「{'、'.join(using)}」正在使用这个服务的向量模型，请先切换后再删除")
    for key in ("default_chat_model", "default_embedding_model", "rerank_model"):
        if str(get_setting(db, key) or "").startswith(prefix):
            set_setting(db, key, None)
    for agent in db.scalars(select(Agent).where(Agent.model_ref.like(prefix + "%"))).all():
        agent.model_ref = None
    db.delete(row)
    db.commit()
    return {"ok": True}


async def _ping_chat(provider, model: str) -> str:
    text = ""
    stream = provider.stream(model, [{"role": "user", "content": "请只回复：ok"}])
    async with aclosing(stream) as parts:
        async for part in parts:
            if part.kind in ("text", "reasoning"):
                text += part.text
                if len(text) >= 40:
                    break
    return text or "（模型没有返回文字）"


@router.post("/api/providers/{provider_id}/test")
async def test_provider(
    provider_id: int, body: TestBody, _admin: User = Depends(admin_user), db: Session = Depends(get_db)
):
    row = _get(db, provider_id)
    infos = models_of(row)
    info = next((m for m in infos if m.model == body.model), None) if body.model else None
    info = info or next((m for m in infos if m.type == "chat"), None) or (infos[0] if infos else None)
    if info is None:
        raise HTTPException(status_code=400, detail="这个服务还没有配置模型")
    started = time.perf_counter()
    try:
        if info.type == "chat":
            sample = await asyncio.wait_for(_ping_chat(chat_provider(row), info.model), timeout=40)
            extra = {"sample": sample[:120]}
        elif info.type == "embedding":
            vectors = await asyncio.to_thread(embedder_from(row, info).embed, ["连接测试"])
            extra = {"dims": int(vectors.shape[1])}
        else:
            order = await asyncio.to_thread(rerank, rerank_config(row, info), "测试", ["测试文档", "无关内容"], 2)
            extra = {"results": len(order)}
    except (ProviderError, httpx.HTTPError, asyncio.TimeoutError, ValueError) as exc:
        return {
            "ok": False,
            "model": info.model,
            "type": info.type,
            "error": str(exc) or exc.__class__.__name__,
            "latency_ms": int((time.perf_counter() - started) * 1000),
        }
    return {
        "ok": True,
        "model": info.model,
        "type": info.type,
        "latency_ms": int((time.perf_counter() - started) * 1000),
        **extra,
    }


def _guess_type(name: str) -> str:
    lower = name.lower()
    if "rerank" in lower:
        return "rerank"
    if "embed" in lower or lower.startswith(("bge-", "baai/bge", "m3e", "gte-")):
        return "embedding"
    return "chat"


@router.post("/api/providers/{provider_id}/models/fetch")
async def fetch_models(provider_id: int, _admin: User = Depends(admin_user), db: Session = Depends(get_db)):
    row = _get(db, provider_id)
    try:
        names = await asyncio.wait_for(chat_provider(row).list_models(), timeout=30)
    except (ProviderError, httpx.HTTPError, asyncio.TimeoutError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=f"获取模型列表失败：{exc}") from exc
    return {"models": [{"id": name, "type": _guess_type(name)} for name in names]}


@router.get("/api/models")
def models(_user: User = Depends(current_user), db: Session = Depends(get_db)):
    return {
        "chat": [m.public() for m in list_models(db, "chat")],
        "embedding": [m.public() for m in list_models(db, "embedding")],
        "rerank": [m.public() for m in list_models(db, "rerank")],
        "defaults": _defaults(db),
    }


@router.put("/api/settings/models")
def save_defaults(body: DefaultsBody, _admin: User = Depends(admin_user), db: Session = Depends(get_db)):
    expected = {"default_chat_model": "chat", "default_embedding_model": "embedding", "rerank_model": "rerank"}
    for key, kind in expected.items():
        ref = getattr(body, key)
        if ref and ref not in (LOCAL_CHAT, LOCAL_EMBED):
            _, info = find_model(db, ref)
            if info is None or info.type != kind:
                raise HTTPException(status_code=400, detail=f"模型 {ref} 不可用")
        set_setting(db, key, ref or None)
    db.commit()
    return _defaults(db)
