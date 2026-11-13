"""旧事件不因智能体改成团队可见、或知识库收回权限而继续交出原文。"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from youju.main import app

PROBE = "探针原文QX7青竹闸门必须先断电"


def _login(username: str) -> TestClient:
    client = TestClient(app)
    admin = TestClient(app)
    assert admin.post("/api/auth/login", json={"username": "admin", "password": "admin-pass-123"}).status_code == 200
    made = admin.post("/api/users", json={"username": username, "password": "member-pass-99182", "display_name": username})
    assert made.status_code in (200, 409), made.text
    assert client.post("/api/auth/login", json={"username": username, "password": "member-pass-99182"}).status_code == 200
    return client


def _agent_body(agent: dict, **changes) -> dict:
    body = {
        "name": agent["name"],
        "emoji": agent["emoji"],
        "color": agent["color"],
        "category": agent["category"],
        "description": agent["description"],
        "system_prompt": agent["system_prompt"],
        "opening": agent["opening"],
        "suggestions": agent["suggestions"],
        "kb_ids": agent["kb_ids"],
        "model_ref": agent["model_ref"],
        "strategy": agent["strategy"],
        "strict": agent["strict"],
        "strictness": agent["strictness"],
        "top_k": agent["top_k"],
        "temperature": agent["temperature"],
        "allow_events": agent["allow_events"],
        "visibility": agent["visibility"],
        "template": agent["template"],
    }
    body.update(changes)
    return body


def _kb_and_agent(owner: TestClient, *, visibility: str) -> tuple[dict, dict]:
    kb = owner.post("/api/kbs", json={"name": f"撤权规程-{visibility}", "visibility": visibility}).json()
    uploaded = owner.post(
        f"/api/kbs/{kb['id']}/documents",
        files=[("files", ("探针.md", f"# 探针\n\n## 处置\n\n青竹闸门检修前必须先断电。{PROBE}。\n".encode(), "text/markdown"))],
    )
    assert uploaded.status_code == 200, uploaded.text
    agent = owner.post(
        "/api/agents",
        json={
            "name": f"撤权助手-{visibility}",
            "kb_ids": [kb["id"]],
            "visibility": visibility,
            "strategy": "rag",
            "strict": False,
            "strictness": "loose",
            "allow_events": True,
        },
    ).json()
    return kb, agent


def _push(owner: TestClient, agent_id: int) -> dict:
    pushed = owner.post(
        f"/api/v1/agents/{agent_id}/events",
        json={"title": "青竹闸门检修前要先做什么", "level": "low", "source": "合成探针", "data": {"note": "只是探针"}},
    )
    assert pushed.status_code == 202, pushed.text
    event = owner.get(f"/api/events/{pushed.json()['id']}").json()
    assert PROBE in event["report"], event["report"]
    assert any(PROBE in (src.get("text") or "") for src in event["sources"])
    return event


def _hide_nested(owner: TestClient, event_id: int, kb_id: int) -> None:
    from youju.db import SessionLocal
    from youju.models import Event

    db = SessionLocal()
    try:
        row = db.get(Event, event_id)
        sources = list(row.sources or [])
        sources.append(
            {
                "n": 9,
                "kb_id": kb_id,
                "filename": "探针.md",
                "section": "处置",
                "text": PROBE,
                "sources": [{"kb_id": kb_id, "text": PROBE, "filename": "嵌套探针.md"}],
            }
        )
        row.sources = sources
        db.commit()
    finally:
        db.close()


def _assert_hidden(payload) -> None:
    blob = json.dumps(payload, ensure_ascii=False)
    assert PROBE not in blob
    assert "嵌套探针.md" not in blob


def test_private_agent_turned_team_hides_old_report(client):
    member = _login("event-revoke-a")
    other = _login("event-revoke-b")
    kb, agent = _kb_and_agent(client, visibility="private")
    event = _push(client, agent["id"])
    _hide_nested(client, event["id"], kb["id"])
    assert "嵌套探针.md" in json.dumps(client.get(f"/api/events/{event['id']}").json(), ensure_ascii=False)
    assert member.get(f"/api/events/{event['id']}").status_code == 404
    opened = client.put(f"/api/agents/{agent['id']}", json=_agent_body(agent, visibility="team")).json()
    assert opened["visibility"] == "team"
    detail = member.get(f"/api/events/{event['id']}")
    assert detail.status_code == 200, detail.text
    _assert_hidden(detail.json())
    assert detail.json()["redacted"] is True
    assert detail.json()["report"]
    listed = member.get("/api/events").json()
    row = next(item for item in listed["events"] if item["id"] == event["id"])
    _assert_hidden(row)
    followed = member.post(f"/api/events/{event['id']}/conversation")
    assert followed.status_code == 200, followed.text
    conv_id = followed.json()["conversation_id"]
    convo = member.get(f"/api/conversations/{conv_id}").json()
    exported = member.get(f"/api/conversations/{conv_id}/export").text
    _assert_hidden(convo)
    assert PROBE not in exported
    assert other.get(f"/api/events/{event['id']}").json()["redacted"] is True
    _assert_hidden(other.get(f"/api/events/{event['id']}").json())
    assert PROBE in client.get(f"/api/events/{event['id']}").json()["report"]


def test_team_kb_made_private_hides_old_report_from_other_member(client):
    owner = _login("event-owner-c")
    other = _login("event-other-d")
    kb, agent = _kb_and_agent(owner, visibility="team")
    event = _push(owner, agent["id"])
    _hide_nested(owner, event["id"], kb["id"])
    visible = other.get(f"/api/events/{event['id']}").json()
    assert PROBE in visible["report"]
    assert "嵌套探针.md" in json.dumps(visible, ensure_ascii=False)
    assert owner.patch(f"/api/kbs/{kb['id']}", json={"visibility": "private"}).status_code == 200
    hidden = other.get(f"/api/events/{event['id']}").json()
    _assert_hidden(hidden)
    listed = json.dumps(other.get("/api/events").json(), ensure_ascii=False)
    assert PROBE not in listed
    conv_id = other.post(f"/api/events/{event['id']}/conversation").json()["conversation_id"]
    assert PROBE not in other.get(f"/api/conversations/{conv_id}/export").text
    fresh = other.post(
        f"/api/v1/agents/{agent['id']}/events",
        json={"title": "青竹闸门检修前要先做什么", "level": "low", "source": "合成探针", "data": {}},
    )
    assert fresh.status_code == 202, fresh.text
    new_event = other.get(f"/api/events/{fresh.json()['id']}").json()
    assert PROBE not in new_event["report"]
    assert PROBE in owner.get(f"/api/events/{event['id']}").json()["report"]
    assert PROBE in client.get(f"/api/events/{event['id']}").json()["report"]
