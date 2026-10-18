import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from youju.db import Base
from youju.models import Agent, Provider, User
from youju.providers import http as provider_http
from youju.providers.adapt import adapt_if_needed, adapt_local_models, choose_probe_model, guess_vision, pick_chat_model, score_chat_model
from youju.providers.registry import ModelInfo
from youju.providers.registry import LOCAL_CHAT, get_setting, set_setting


def test_guess_vision_covers_agent_plan_multimodal():
    assert guess_vision("ark-code-latest")
    assert guess_vision("doubao-seed-2.1-pro")
    assert guess_vision("qwen3-vl:30b")
    assert not guess_vision("deepseek-chat")
    assert not guess_vision("qwen3.5:9b")


def test_pick_chat_prefers_local_midsize_over_cloud():
    names = ["qwen3-vl:235b-cloud", "llama3.1:8b", "nomic-embed-text", "qwen3.5:9b"]
    assert pick_chat_model(names) in {"llama3.1:8b", "qwen3.5:9b"}
    assert pick_chat_model(names, preferred="llama3.1:8b") == "llama3.1:8b"
    assert score_chat_model("huihui_ai/qwen3-abliterated:8b") > score_chat_model("qwen3-vl:235b-cloud")


def _info(model: str, kind: str = "chat") -> ModelInfo:
    return ModelInfo(f"1:{model}", model, kind, 1, "服务", "ollama", "openai", "#000")


def test_choose_probe_skips_cloud_and_unopened_preset():
    local = choose_probe_model(
        [_info("deepseek-v3.1:671b-cloud"), _info("glm-4.6:cloud"), _info("qwen3.5:9b"), _info("bge-m3", "embedding")],
        ["deepseek-v3.1:671b-cloud", "glm-4.6:cloud", "qwen3.5:9b", "bge-m3"],
    )
    assert local.model == "qwen3.5:9b"
    ark = choose_probe_model(
        [_info("doubao-seed-1-6-250615"), _info("deepseek-r1-250120")],
        ["deepseek-r1-250120", "deepseek-r1-250528"],
    )
    assert ark.model == "deepseek-r1-250120"
    endpoint = choose_probe_model(
        [_info("deepseek-r1-250120"), _info("ep-202610170001")],
        ["deepseek-r1-250120"],
    )
    assert endpoint.model == "ep-202610170001"


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _provider(db: Session, models=None) -> Provider:
    user = User(username="a", display_name="a", password_hash="x", role="admin")
    db.add(user)
    db.flush()
    row = Provider(
        preset="ollama",
        name="Ollama（本地）",
        kind="openai",
        base_url="http://127.0.0.1:11434/v1",
        api_key="",
        extra_headers={},
        models=models or [{"id": "qwen3:8b", "type": "chat", "tools": True, "vision": False}],
        options={},
        enabled=True,
    )
    db.add(row)
    db.flush()
    db.add(
        Agent(
            name="安全巡检助手",
            owner_id=user.id,
            model_ref=f"{row.id}:qwen3:8b",
        )
    )
    set_setting(db, "default_chat_model", f"{row.id}:qwen3:8b")
    db.commit()
    db.refresh(row)
    return row


def test_adapt_rewrites_missing_names_to_whatever_is_installed():
    def handler(req):
        assert str(req.url).endswith("/models")
        return httpx.Response(
            200,
            json={"data": [{"id": "llama3.1:8b"}, {"id": "nomic-embed-text"}, {"id": "qwen3-vl:235b-cloud"}]},
        )

    provider_http.transport_override = httpx.MockTransport(handler)
    db = _session()
    try:
        row = _provider(db)
        result = adapt_local_models(db)
        db.refresh(row)
        ids = [item["id"] for item in row.models]
        assert "llama3.1:8b" in ids
        assert "qwen3:8b" not in ids
        assert any(item["id"] == "nomic-embed-text" and item["type"] == "embedding" for item in row.models)
        assert get_setting(db, "default_chat_model") == f"{row.id}:llama3.1:8b"
        agent = db.query(Agent).one()
        assert agent.model_ref is None
        assert result["ok"] and result["synced"][0]["chat"] == "llama3.1:8b"
    finally:
        provider_http.transport_override = None
        db.close()


def test_adapt_keeps_config_when_ollama_is_down():
    def handler(_req):
        return httpx.Response(502, json={"error": "proxy"})

    provider_http.transport_override = httpx.MockTransport(handler)
    db = _session()
    try:
        row = _provider(db)
        result = adapt_local_models(db)
        db.refresh(row)
        assert result["ok"] is False
        assert row.models[0]["id"] == "qwen3:8b"
        assert get_setting(db, "default_chat_model") == f"{row.id}:qwen3:8b"
    finally:
        provider_http.transport_override = None
        db.close()


def test_adapt_creates_ollama_provider_on_fresh_machine():
    def handler(_req):
        return httpx.Response(200, json={"data": [{"id": "phi3:mini"}]})

    provider_http.transport_override = httpx.MockTransport(handler)
    db = _session()
    try:
        result = adapt_local_models(db)
        row = db.query(Provider).one()
        assert row.preset == "ollama"
        assert row.models[0]["id"] == "phi3:mini"
        assert get_setting(db, "default_chat_model") == f"{row.id}:phi3:mini"
        assert result["ok"] and result["created"]
    finally:
        provider_http.transport_override = None
        db.close()


def test_adapt_if_needed_respects_disable_flag(monkeypatch):
    monkeypatch.setenv("YOUJU_ADAPT_LOCAL", "0")
    db = _session()
    try:
        assert adapt_if_needed(db) == {"skipped": True}
        assert db.query(Provider).count() == 0
    finally:
        db.close()


def test_adapt_does_not_override_explicit_offline_default():
    def handler(_req):
        return httpx.Response(200, json={"data": [{"id": "llama3.1:8b"}]})

    provider_http.transport_override = httpx.MockTransport(handler)
    db = _session()
    try:
        row = _provider(db)
        set_setting(db, "default_chat_model", LOCAL_CHAT)
        db.commit()
        adapt_local_models(db)
        assert get_setting(db, "default_chat_model") == LOCAL_CHAT
        db.refresh(row)
        assert row.models[0]["id"] == "llama3.1:8b"
    finally:
        provider_http.transport_override = None
        db.close()
