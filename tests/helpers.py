import json

import httpx


def read_sse(resp) -> list[dict]:
    events = []
    for line in resp.text.splitlines():
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload and payload != "[DONE]":
                events.append(json.loads(payload))
    return events


def sse_body(*objs, done: bool = True) -> bytes:
    body = "".join(f"data: {json.dumps(o, ensure_ascii=False)}\n\n" for o in objs)
    if done:
        body += "data: [DONE]\n\n"
    return body.encode("utf-8")


def sse_response(*objs, done: bool = True) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse_body(*objs, done=done))


def ask(client, agent_id: int, content: str, model: str | None = "local:extractive", **extra):
    conv = client.post("/api/conversations", json={"agent_id": agent_id}).json()
    resp = client.post(f"/api/conversations/{conv['id']}/chat", json={"content": content, "model": model, **extra})
    assert resp.status_code == 200, resp.text
    events = read_sse(resp)
    final = next(e for e in events if e["type"] == "final")
    return conv, events, final
