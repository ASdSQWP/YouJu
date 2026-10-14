from fastapi.testclient import TestClient

from helpers import ask, read_sse
from youju.main import app


def test_status_and_setup_only_once(client):
    status = client.get("/api/auth/status").json()
    assert status["initialized"] and status["user"]["role"] == "admin"
    assert client.post("/api/auth/setup", json={"username": "again", "password": "123456"}).status_code == 409


def test_seed_content_is_indexed(agents, kbs):
    assert {"安全生产规程", "IT 运维手册", "员工制度手册", "客户服务知识库"} <= set(kbs)
    assert all(kb["doc_count"] > 0 and kb["ready_count"] == kb["doc_count"] for kb in kbs.values())
    assert {"safety", "itops", "hr", "service", "general"} <= set(agents)


def test_every_seed_eval_set_hits_top3(client, kbs):
    for kb in kbs.values():
        run = client.post(f"/api/kbs/{kb['id']}/eval/run", json={"top_k": 3}).json()
        missed = [d for d in run["details"] if not d["rank"]]
        assert run["metrics"]["hit_at_k"] == 1.0, (kb["name"], missed)
        assert run["metrics"]["evidence_rate"] == 1.0, kb["name"]


def test_search_gate(client, kbs):
    kb_id = kbs["安全生产规程"]["id"]
    good = client.post(f"/api/kbs/{kb_id}/search", json={"query": "安全带没扣好可以继续干活吗"}).json()
    assert good["evidence"] and good["hits"][0]["filename"] == "高空作业安全规程.md"
    assert {"dense", "bm25", "rrf", "coverage"} <= set(good["hits"][0]["scores"])
    bad = client.post(f"/api/kbs/{kb_id}/search", json={"query": "明天广州天气怎么样"}).json()
    assert not bad["evidence"]


def test_offline_answer_streams_with_citations(client, agents):
    conv, events, final = ask(client, agents["safety"]["id"], "安全带没扣好可以继续干活吗？")
    kinds = [e["type"] for e in events]
    assert kinds[0] == "start" and kinds[1] == "meta" and "sources" in kinds and kinds[-1] == "done"
    assert final["mode"] == "offline" and final["used"]
    assert "停止作业" in final["content"]
    assert final["check"]["verdict"] == "pass", final["check"]
    detail = client.get(f"/api/conversations/{conv['id']}").json()
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]
    assert detail["messages"][1]["sources"] and detail["messages"][1]["check"]["verdict"] == "pass"
    assert detail["conversation"]["title"].startswith("安全带")


def test_strict_agent_refuses_without_evidence(client, agents):
    _, _, final = ask(client, agents["safety"]["id"], "明天广州天气怎么样？")
    assert final["refused"] and final["used"] == [] and "暂不作答" in final["content"]
    assert any(step["kind"] == "guard" for step in final["trace"])


def test_smalltalk_and_offline_general_agent(client, agents):
    _, _, hello = ask(client, agents["hr"]["id"], "你好")
    assert not hello["refused"] and "制度问答助手" in hello["content"]
    _, _, general = ask(client, agents["general"]["id"], "写一首诗")
    assert "还没有接入大模型" in general["content"]


def test_follow_up_question_uses_previous_turn(client, agents):
    conv, _, _ = ask(client, agents["hr"]["id"], "去上海出差住宿标准是多少？")
    resp = client.post(f"/api/conversations/{conv['id']}/chat", json={"content": "那其他城市呢？", "model": "local:extractive"})
    final = next(e for e in read_sse(resp) if e["type"] == "final")
    assert "350 元" in final["content"]


def test_regenerate_feedback_and_export(client, agents):
    conv, _, _ = ask(client, agents["hr"]["id"], "工作满一年有几天年假？")
    resp = client.post(f"/api/conversations/{conv['id']}/chat", json={"regenerate": True, "model": "local:extractive"})
    final = next(e for e in read_sse(resp) if e["type"] == "final")
    assert "5 天" in final["content"]
    msgs = client.get(f"/api/conversations/{conv['id']}").json()["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    rated = client.post(f"/api/messages/{final['message_id']}/feedback", json={"rating": -1, "note": "测试差评"})
    assert rated.json()["feedback"] == -1
    exported = client.get(f"/api/conversations/{conv['id']}/export")
    assert exported.status_code == 200 and "参考资料" in exported.text
    kb_id = agents["hr"]["kb_ids"][0]
    case = client.post(f"/api/messages/{final['message_id']}/to-eval", json={"kb_id": kb_id, "expected": "考勤与请假制度.md"})
    assert case.json()["question"] == "工作满一年有几天年假？"


def test_upload_parse_search_and_delete(client):
    kb = client.post("/api/kbs", json={"name": "测试知识库", "visibility": "private"}).json()
    md = "# 说明\n\n## 门禁\n\n门禁卡丢失要在当天到前台补办。".encode()
    files = [
        ("files", ("制度.txt", "第一章 报销\n报销需要在 30 天内提交。".encode("gbk"), "text/plain")),
        ("files", ("说明.md", md, "text/markdown")),
        ("files", ("程序.exe", b"MZ", "application/octet-stream")),
    ]
    uploaded = client.post(f"/api/kbs/{kb['id']}/documents", files=files).json()
    assert len(uploaded["created"]) == 2 and uploaded["skipped"][0]["filename"] == "程序.exe"
    docs = client.get(f"/api/kbs/{kb['id']}/documents").json()["documents"]
    assert {d["status"] for d in docs} == {"ready"}
    duplicate = client.post(f"/api/kbs/{kb['id']}/documents", files=[("files", ("副本.md", md, "text/markdown"))]).json()
    assert not duplicate["created"] and "相同" in duplicate["skipped"][0]["reason"]
    hit = client.post(f"/api/kbs/{kb['id']}/search", json={"query": "门禁卡丢了怎么办"}).json()["hits"][0]
    assert hit["filename"] == "说明.md" and hit["section"] == "门禁"
    doc = next(d for d in docs if d["filename"] == "说明.md")
    assert client.get(f"/api/documents/{doc['id']}/chunks").json()["chunks"][0]["section"] == "门禁"
    assert client.get(f"/api/documents/{doc['id']}/file").content == md
    assert client.delete(f"/api/documents/{doc['id']}").json()["ok"]
    assert client.delete(f"/api/kbs/{kb['id']}").json()["ok"]
    assert client.get(f"/api/kbs/{kb['id']}").status_code == 404


def test_member_permissions(client, kbs):
    created = client.post("/api/users", json={"username": "member1", "password": "member-pass", "display_name": "成员一"})
    assert created.status_code == 200
    private = client.post("/api/kbs", json={"name": "管理员私有库", "visibility": "private"}).json()
    other = TestClient(app)
    assert other.get("/api/kbs").status_code == 401
    assert other.post("/api/auth/login", json={"username": "member1", "password": "wrong"}).status_code == 401
    assert other.post("/api/auth/login", json={"username": "member1", "password": "member-pass"}).status_code == 200
    names = {kb["name"] for kb in other.get("/api/kbs").json()["kbs"]}
    assert "管理员私有库" not in names and "安全生产规程" in names
    assert other.get(f"/api/kbs/{private['id']}").status_code == 404
    assert other.post(f"/api/kbs/{kbs['安全生产规程']['id']}/reindex").status_code == 403
    assert other.get("/api/users").status_code == 403
    assert other.get("/api/providers").status_code == 403
    assert other.get("/api/stats").status_code == 403
    mine = other.post("/api/kbs", json={"name": "成员自己的库", "visibility": "private"}).json()
    assert mine["can_edit"]
    other.post("/api/auth/logout")
    assert other.get("/api/auth/status").json()["user"] is None
    client.delete(f"/api/kbs/{private['id']}")
    client.delete(f"/api/kbs/{mine['id']}")


def test_event_webhook_review_and_follow_up(client, agents):
    key = client.post("/api/apikeys", json={"name": "监控系统"}).json()["key"]
    headers = {"Authorization": f"Bearer {key}"}
    pushed = client.post(
        f"/api/v1/agents/{agents['safety']['id']}/events",
        headers=headers,
        json={"title": "杆塔3 检测到安全带缺失", "level": "high", "source": "视觉检测", "data": {"findings": ["安全带缺失", "挂钩未连接"]}},
    )
    assert pushed.status_code == 202
    event = client.get(f"/api/events/{pushed.json()['id']}").json()
    assert event["status"] == "pending" and event["sources"]
    assert "停止作业" in event["report"] and "### 处置步骤" in event["report"]
    assert event["check"]["verdict"] in ("pass", "warn")
    reviewed = client.post(f"/api/events/{event['id']}/review", json={"action": "approve", "comment": "已现场确认"}).json()
    assert reviewed["status"] == "approved" and reviewed["review_comment"] == "已现场确认"
    conv_id = client.post(f"/api/events/{event['id']}/conversation").json()["conversation_id"]
    assert client.get(f"/api/conversations/{conv_id}").json()["messages"][1]["content"] == event["report"]
    assert client.post(f"/api/v1/agents/{agents['hr']['id']}/events", json={"title": "x"}).status_code == 400
    bad = TestClient(app).post(f"/api/v1/agents/{agents['safety']['id']}/events", headers={"Authorization": "Bearer yj-nope"}, json={"title": "x"})
    assert bad.status_code == 401


def test_simulated_events_route_to_template_agents(client, agents):
    samples = {s["key"] for s in client.get("/api/events/samples").json()["samples"]}
    assert {"safety-height", "itops-pool", "service-delay"} <= samples
    sim = client.post("/api/events/simulate", json={"sample": "itops-pool"}).json()
    event = client.get(f"/api/events/{sim['id']}").json()
    assert event["agent_id"] == agents["itops"]["id"] and event["status"] == "pending"
    assert "连接" in event["report"]
    assert client.get("/api/events").json()["counts"]["pending"] >= 1


def test_openai_compatible_api(client, agents):
    key = client.post("/api/apikeys", json={"name": "openai"}).json()["key"]
    headers = {"Authorization": f"Bearer {key}"}
    ids = {m["id"] for m in client.get("/v1/models", headers=headers).json()["data"]}
    assert f"agent-{agents['hr']['id']}" in ids
    plain = client.post(
        "/v1/chat/completions",
        headers=headers,
        json={"model": f"agent-{agents['hr']['id']}", "messages": [{"role": "user", "content": "去上海出差住宿标准是多少？"}]},
    ).json()
    content = plain["choices"][0]["message"]["content"]
    assert "500 元" in content and "参考资料" in content and plain["youju"]["sources"]
    stream = client.post(
        "/v1/chat/completions",
        headers=headers,
        json={"model": "制度问答助手", "stream": True, "messages": [{"role": "user", "content": "试用期多长？"}]},
    )
    chunks = read_sse(stream)
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks)
    assert "6 个月" in text and chunks[-1]["choices"][0]["finish_reason"] == "stop"
    assert stream.text.rstrip().endswith("data: [DONE]")
    assert client.post("/v1/chat/completions", headers=headers, json={"model": "nope", "messages": [{"role": "user", "content": "x"}]}).status_code == 404


def test_mcp_server(client):
    key = client.post("/api/apikeys", json={"name": "cursor"}).json()["key"]
    mcp = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    def rpc(method, params=None, msg_id=1):
        body = {"jsonrpc": "2.0", "id": msg_id, "method": method}
        if params is not None:
            body["params"] = params
        return mcp.post("/mcp", json=body).json()

    init = rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "pytest", "version": "1"}})
    assert init["result"]["protocolVersion"] == "2025-06-18" and init["result"]["capabilities"]["tools"]
    assert mcp.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}).status_code == 202
    tools = {t["name"] for t in rpc("tools/list")["result"]["tools"]}
    assert tools == {"list_knowledge_bases", "search_knowledge", "ask_agent"}
    found = rpc("tools/call", {"name": "search_knowledge", "arguments": {"query": "磁盘满了哪些文件可以删", "knowledge_base": "IT 运维手册"}})["result"]
    assert not found["isError"] and "磁盘空间告警处置.md" in found["content"][0]["text"]
    answer = rpc("tools/call", {"name": "ask_agent", "arguments": {"agent": "客服助手", "question": "生鲜可以退吗？"}})["result"]
    assert "生鲜" in answer["content"][0]["text"] and answer["structuredContent"]["sources"]
    assert rpc("tools/call", {"name": "ask_agent", "arguments": {"agent": "不存在", "question": "x"}})["result"]["isError"]
    assert rpc("unknown/method")["error"]["code"] == -32601
    assert mcp.get("/mcp").status_code == 405
    assert TestClient(app).post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 401


def test_stats_dashboard(client):
    data = client.get("/api/stats").json()
    assert data["totals"]["answers"] > 0 and data["summary"]["answers"] > 0
    assert any(g["question"].startswith("明天广州天气") for g in data["gaps"])
    assert data["feedback"]["down"] >= 1 and data["negative"][0]["note"] == "测试差评"
    assert data["checks"].get("pass", 0) >= 1
