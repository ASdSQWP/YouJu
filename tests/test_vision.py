from youju.vision.compose import parse_instruction


def test_compose_natural_language_rule():
    sources = [{"id": 3, "name": "杆塔3-登高作业", "code": "cam-03"}]
    agents = [{"id": 1, "name": "安全巡检助手", "template": "safety"}]
    draft = parse_instruction("给杆塔3拉流，检测未系安全带和挂钩，置信度 0.6 以上告警，推给安全巡检助手", sources, agents)
    assert draft["source_id"] == 3
    assert draft["algorithms"] == ["no_belt", "no_hook"]
    assert draft["confidence"] == 0.6
    assert draft["auto_pull"] and draft["auto_push"]
    assert draft["agent_id"] == 1
    assert draft["detector"] == "small"


def test_vision_seed_pull_and_alert(client, agents):
    catalog = client.get("/api/vision/catalog").json()
    assert {a["id"] for a in catalog["algorithms"]} >= {"no_belt", "fire_smoke", "no_helmet"}
    sources = client.get("/api/vision/sources").json()["sources"]
    names = {s["name"] for s in sources}
    assert {"杆塔3-登高作业", "动火区-电缆沟", "工地大门", "限界监测-吊臂"} <= names
    rules = client.get("/api/vision/rules").json()["rules"]
    assert any(r["source_name"] == "杆塔3-登高作业" and "no_belt" in r["algorithms"] for r in rules)

    preview = client.post("/api/vision/rules/compose", json={"instruction": "工地大门检测未戴安全帽", "apply": False}).json()
    assert preview["draft"]["algorithms"] == ["no_helmet"]
    assert preview["applied"] is None

    pulled = client.post("/api/vision/pull", json={"name": "杆塔3"}).json()
    assert pulled["live"] and pulled["name"] == "杆塔3-登高作业"
    assert pulled["boxes"]
    frame = client.get(f"/api/vision/sources/{pulled['id']}/frame")
    assert frame.status_code == 200 and frame.content[:8] == b"\x89PNG\r\n\x1a\n"

    alerts = client.get("/api/vision/alerts").json()["alerts"]
    tower = [a for a in alerts if a["source_id"] == pulled["id"]]
    assert tower and "安全带" in tower[0]["title"]
    assert tower[0]["event_id"]
    event = client.get(f"/api/events/{tower[0]['event_id']}").json()
    assert event["source"].startswith("视觉检测")
    assert event["status"] in ("pending", "processing", "failed")
    client.post(f"/api/vision/sources/{pulled['id']}/stop")


def test_manual_rule_and_upload(client, agents):
    sources = {s["name"]: s for s in client.get("/api/vision/sources").json()["sources"]}
    gate = sources["工地大门"]
    created = client.post(
        "/api/vision/rules",
        json={
            "name": "大门安全帽加严",
            "source_id": gate["id"],
            "algorithms": ["no_helmet"],
            "confidence": 0.7,
            "level": "medium",
            "detector": "vlm",
            "agent_id": agents["safety"]["id"],
            "auto_push": False,
        },
    ).json()
    assert created["name"] == "大门安全帽加严" and created["detector"] == "vlm"
    patched = client.patch(f"/api/vision/rules/{created['id']}", json={"enabled": False}).json()
    assert patched["enabled"] is False
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    uploaded = client.post("/api/vision/sources/upload", files=[("files", ("现场.jpg", png, "image/jpeg"))]).json()
    assert uploaded["created"][0]["name"] == "现场"
    src_id = uploaded["created"][0]["id"]
    assert client.get(f"/api/vision/sources/{src_id}/media").status_code == 200
    client.delete(f"/api/vision/sources/{src_id}")
    client.delete(f"/api/vision/rules/{created['id']}")
