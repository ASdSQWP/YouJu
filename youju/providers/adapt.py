"""按本机实际可用的本地模型对齐配置，换电脑不用改模型名。"""

from __future__ import annotations

import logging
import os
import time

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from youju.models import Agent, Provider
from youju.providers import http
from youju.providers.presets import PRESET_MAP
from youju.providers.registry import LOCAL_CHAT, get_setting, set_setting

log = logging.getLogger("youju.adapt")

OLLAMA_URL = "http://127.0.0.1:11434/v1"
PROBE_TIMEOUT = httpx.Timeout(connect=1.5, read=5.0, write=5.0, pool=1.5)
_OK_TTL = 600.0
_FAIL_TTL = 3.0
_state = {"ok": False, "tried_at": 0.0}


def _enabled() -> bool:
    value = os.getenv("YOUJU_ADAPT_LOCAL")
    if value is None:
        return True
    return value.strip().lower() not in {"0", "false", "no", "off"}


def guess_model_type(name: str) -> str:
    lower = name.lower()
    if "rerank" in lower:
        return "rerank"
    if "embed" in lower or lower.startswith(("bge-", "baai/bge", "m3e", "gte-", "nomic-embed")):
        return "embedding"
    return "chat"


def model_entry(name: str) -> dict:
    kind = guess_model_type(name)
    data: dict = {"id": name, "type": kind}
    if kind == "chat":
        data["tools"] = True
        data["vision"] = "vl" in name.lower() or "vision" in name.lower()
    return data


def score_chat_model(name: str) -> int:
    """本机默认模型：优先中小尺寸本地对话模型，避开云端和超大模型。"""
    lower = name.lower()
    score = 0
    if "cloud" in lower:
        score -= 50
    if any(token in lower for token in ("vl", "vision")):
        score -= 6
    for token, pts in (
        ("qwen", 18),
        ("llama", 14),
        ("gemma", 12),
        ("mistral", 12),
        ("phi", 10),
        ("glm", 8),
        ("deepseek", 8),
        ("yi", 8),
        ("gpt-oss", 6),
    ):
        if token in lower:
            score += pts
            break
    for token, pts in (
        (":8b", 15),
        (":7b", 14),
        (":9b", 14),
        (":4b", 10),
        (":3b", 9),
        (":14b", 7),
        (":12b", 7),
        (":13b", 6),
        (":11b", 6),
        (":1.5b", 5),
        (":1b", 4),
    ):
        if token in lower:
            score += pts
            break
    if any(token in lower for token in (":20b", ":27b", ":30b", ":32b", ":34b", ":70b", ":72b", ":235b", ":671b")):
        score -= 12
    return score


def pick_chat_model(names: list[str], preferred: str | None = None) -> str | None:
    chats = [name for name in names if guess_model_type(name) == "chat"]
    if preferred and preferred in chats:
        return preferred
    if not chats:
        return None
    return sorted(chats, key=lambda name: (-score_chat_model(name), name))[0]


def is_local_provider(row: Provider) -> bool:
    if not row.enabled:
        return False
    if row.preset == "ollama":
        return True
    url = (row.base_url or "").lower()
    return row.kind == "openai" and any(token in url for token in ("127.0.0.1", "localhost", "[::1]"))


def fetch_model_ids(base_url: str, headers: dict | None = None) -> list[str] | None:
    url = f"{base_url.rstrip('/')}/models"
    try:
        with http.sync_client(PROBE_TIMEOUT) as client:
            resp = client.get(url, headers=headers or {"Content-Type": "application/json"})
    except (httpx.HTTPError, OSError) as exc:
        log.info("本机模型探测失败 %s：%s", url, exc)
        return None
    if resp.status_code >= 400:
        log.info("本机模型探测 HTTP %s：%s", resp.status_code, url)
        return None
    try:
        payload = resp.json()
    except ValueError:
        return None
    items = payload.get("data") if isinstance(payload, dict) else payload
    names = sorted({str(item["id"]) for item in items or [] if isinstance(item, dict) and item.get("id")})
    return names or None


def _headers_of(row: Provider) -> dict:
    headers = {"Content-Type": "application/json"}
    key = row.api_key or ""
    auth = (row.options or {}).get("auth_header") or "authorization"
    if key:
        if auth == "api-key":
            headers["api-key"] = key
        else:
            headers["Authorization"] = f"Bearer {key}"
    headers.update(row.extra_headers or {})
    return headers


def _merge_models(row: Provider, names: list[str]) -> list[dict]:
    live = [model_entry(name) for name in names]
    live_ids = {item["id"] for item in live}
    kept = [
        item
        for item in (row.models or [])
        if isinstance(item, dict) and item.get("type") in ("embedding", "rerank") and item.get("id") not in live_ids
    ]
    return live + kept


def _apply(db: Session, row: Provider, names: list[str]) -> str | None:
    row.models = _merge_models(row, names)
    flag_modified(row, "models")
    chat_ids = {item["id"] for item in row.models if item.get("type") == "chat"}
    current = get_setting(db, "default_chat_model")
    preferred = None
    pid = None
    if isinstance(current, str) and current not in (LOCAL_CHAT,):
        pid, _, mid = current.partition(":")
        if pid == str(row.id):
            preferred = mid
    chosen = pick_chat_model(list(chat_ids), preferred)
    missing_here = pid == str(row.id) and preferred not in chat_ids
    if chosen and (not current or missing_here):
        set_setting(db, "default_chat_model", f"{row.id}:{chosen}")
    prefix = f"{row.id}:"
    for agent in db.scalars(select(Agent).where(Agent.model_ref.like(prefix + "%"))).all():
        mid = (agent.model_ref or "").partition(":")[2]
        if mid not in chat_ids:
            agent.model_ref = None
    return chosen


def _ensure_ollama(db: Session) -> Provider | None:
    names = fetch_model_ids(OLLAMA_URL)
    if not names:
        return None
    preset = PRESET_MAP["ollama"]
    row = Provider(
        preset="ollama",
        name=preset["name"],
        kind=preset["kind"],
        base_url=preset["base_url"],
        api_key="",
        extra_headers={},
        models=[model_entry(name) for name in names],
        options={"stream_usage": False, "embed_batch": int(preset.get("embed_batch", 16)), "auth_header": "authorization"},
        enabled=True,
    )
    db.add(row)
    db.flush()
    chosen = pick_chat_model(names)
    current = get_setting(db, "default_chat_model")
    if chosen and not current:
        set_setting(db, "default_chat_model", f"{row.id}:{chosen}")
    log.info("已自动接入本机 Ollama，默认模型 %s", chosen)
    return row


def adapt_local_models(db: Session) -> dict:
    """读取本机 Ollama（或其它本机兼容接口）的真实模型名，改写默认模型和智能体绑定。"""
    synced = []
    locals_ = [row for row in db.scalars(select(Provider).order_by(Provider.id)).all() if is_local_provider(row)]
    if not locals_:
        created = _ensure_ollama(db)
        if created is None:
            return {"ok": False, "reason": "no_local_llm"}
        db.commit()
        return {"ok": True, "created": True, "chat": pick_chat_model([m["id"] for m in created.models if m.get("type") == "chat"])}

    for row in locals_:
        names = fetch_model_ids(row.base_url, _headers_of(row))
        if not names:
            continue
        chosen = _apply(db, row, names)
        synced.append({"provider_id": row.id, "chat": chosen, "count": len(names)})
        log.info("已按本机模型对齐 %s，共 %s 个，默认 %s", row.name, len(names), chosen)
    if not synced:
        return {"ok": False, "reason": "unreachable"}
    db.commit()
    return {"ok": True, "synced": synced}


def adapt_if_needed(db: Session) -> dict:
    if not _enabled():
        return {"skipped": True}
    now = time.monotonic()
    ttl = _OK_TTL if _state["ok"] else _FAIL_TTL
    if _state["tried_at"] and now - _state["tried_at"] < ttl:
        return {"skipped": True, "cached": True}
    _state["tried_at"] = now
    try:
        result = adapt_local_models(db)
    except Exception:  # noqa: BLE001
        log.exception("本机模型自适应失败")
        _state["ok"] = False
        return {"ok": False, "reason": "error"}
    _state["ok"] = bool(result.get("ok"))
    return result
