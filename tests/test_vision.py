from youju.vision.compose import merge_model_json, parse_instruction
from youju.vision.engine import detect_frame, match_rule, visible_objects
from youju.models import DetectionRule, VisionSource


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


def test_compose_postprocess_detector():
    draft = parse_instruction("检测烟火，用小模型加大模型后处理", [], [])
    assert draft["detector"] == "small_then_vlm"
    assert "fire_smoke" in draft["algorithms"]


def test_merge_model_json_algorithms():
    base = parse_instruction("检测未戴安全帽", [], [])
    merged = merge_model_json(base, '{"algorithms":["no_helmet","fire_smoke"],"detector":"vlm","confidence":0.8}')
    assert merged["algorithms"] == ["no_helmet", "fire_smoke"]
    assert merged["detector"] == "vlm"
    assert merged["confidence"] == 0.8
    assert merged["mode"] == "model"


def test_algorithm_filter_on_detect_frame():
    src = VisionSource(id=1, owner_id=1, name="t", scenario="tower", status="live", kind="demo")
    rule = DetectionRule(
        id=9,
        owner_id=1,
        name="只查安全带",
        source_id=1,
        algorithms=["no_belt"],
        confidence=0.5,
        detector="small",
        enabled=True,
    )
    objects = visible_objects("tower", 5)
    assert any(o["id"] == "no_hook" for o in objects)
    hits = match_rule(rule, src, objects)
    assert hits and all(h["id"] == "no_belt" for h in hits)
    boxes, skipped, _hint, _jpeg = detect_frame(src, [rule], 5, vlm_ok=False)
    assert boxes and all(b["id"] == "no_belt" for b in boxes)
    assert skipped is False


def test_vlm_without_model_does_not_invent_alerts():
    src = VisionSource(id=2, owner_id=1, name="gate", scenario="gate", status="live", kind="demo")
    rule = DetectionRule(
        id=10,
        owner_id=1,
        name="大门",
        source_id=2,
        algorithms=["no_helmet"],
        confidence=0.5,
        detector="vlm",
        enabled=True,
    )
    boxes, skipped, hint, _jpeg = detect_frame(src, [rule], 5, vlm_ok=False)
    assert skipped is True
    assert "大模型" in hint
    assert boxes == []


def test_capture_jpeg_from_image(tmp_path):
    from PIL import Image

    from youju.vision.frames import capture_jpeg

    path = tmp_path / "scene.png"
    Image.new("RGB", (64, 48), color=(20, 80, 40)).save(path)
    jpeg = capture_jpeg(path)
    assert jpeg and jpeg[:2] == b"\xff\xd8"


def test_parse_vlm_findings_requires_reason():
    from youju.vision.vlm import _parse_findings

    text = '{"findings":[{"id":"no_helmet","confidence":0.9,"box":[0.1,0.2,0.3,0.4],"reason":"画面中工人未戴安全帽"}]}'
    hits = _parse_findings(text, ["no_helmet"], rule_id=1, detector="vlm")
    assert len(hits) == 1 and hits[0]["from_image"] and hits[0]["reason"]
    empty = _parse_findings('{"findings":[{"id":"no_helmet","confidence":0.9,"box":[0,0,1,1]}]}', ["no_helmet"], 1, "vlm")
    assert empty == []


def test_vision_seed_pull_and_alert(client, agents):
    catalog = client.get("/api/vision/catalog").json()
    assert {a["id"] for a in catalog["algorithms"]} >= {"no_belt", "fire_smoke", "no_helmet"}
    assert {d["id"] for d in catalog["detectors"]} >= {"small", "vlm", "small_then_vlm"}
    assert catalog["categories"]
    sources = client.get("/api/vision/sources").json()["sources"]
    names = {s["name"] for s in sources}
    assert {"杆塔3-登高作业", "动火区-电缆沟", "工地大门", "限界监测-吊臂"} <= names
    assert all("visibility" in s and "category" in s for s in sources)
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
    uploaded = client.post(
        "/api/vision/sources/upload",
        files=[("files", ("现场.jpg", png, "image/jpeg"))],
        data={"category": "gate", "visibility": "team", "shared_user_ids": "[]"},
    ).json()
    assert uploaded["created"][0]["name"] == "现场"
    assert uploaded["created"][0]["category"] == "gate"
    assert uploaded["created"][0]["visibility"] == "team"
    src_id = uploaded["created"][0]["id"]
    assert client.get(f"/api/vision/sources/{src_id}/media").status_code == 200
    grouped = client.get("/api/vision/sources?group=owner").json()
    assert grouped["groups"] and any(g["sources"] for g in grouped["groups"])
    session = client.post(
        f"/api/vision/sources/{src_id}/session-rule",
        json={"algorithms": ["no_helmet"], "detector": "small"},
    ).json()
    assert session["source_id"] == src_id and "no_helmet" in session["algorithms"]
    status = client.get("/api/vision/vlm-status").json()
    assert "ready" in status
    directory = client.get("/api/users/directory").json()
    assert directory["users"]
    client.delete(f"/api/vision/sources/{src_id}")
    client.delete(f"/api/vision/rules/{created['id']}")


def test_visibility_private_hides_from_other_user(client):
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    uploaded = client.post(
        "/api/vision/sources/upload",
        files=[("files", ("私有.jpg", png, "image/jpeg"))],
        data={"category": "other", "visibility": "private", "shared_user_ids": "[]"},
    ).json()
    src_id = uploaded["created"][0]["id"]
    client.post("/api/users", json={"username": "viewer1", "display_name": "访客", "password": "viewer-pass-1", "role": "member"})
    # 用第二个客户端登录访客
    from fastapi.testclient import TestClient
    from youju.main import app

    with TestClient(app) as other:
        login = other.post("/api/auth/login", json={"username": "viewer1", "password": "viewer-pass-1"})
        assert login.status_code == 200, login.text
        visible = {s["id"] for s in other.get("/api/vision/sources").json()["sources"]}
        assert src_id not in visible
        assert other.get(f"/api/vision/sources/{src_id}/preview").status_code == 404
    client.delete(f"/api/vision/sources/{src_id}")


def _jpeg_bytes(color=(30, 90, 40), size=(96, 72)):
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, color=color).save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def test_catalog_lists_small_and_vision_models(client):
    catalog = client.get("/api/vision/catalog").json()
    ids = {m["id"] for m in catalog["small_models"]}
    assert "demo" in ids
    assert any(i == "yolo" or i.startswith("yolo:") for i in ids)
    assert "vision_models" in catalog


def test_yolo_without_weights_does_not_invent(tmp_path, monkeypatch):
    from PIL import Image

    from youju.models import DetectionRule, VisionSource
    from youju.vision.engine import detect_frame

    monkeypatch.delenv("YOUJU_YOLO_MODEL", raising=False)
    path = tmp_path / "scene.jpg"
    Image.new("RGB", (40, 30), color=(8, 8, 8)).save(path, format="JPEG")
    src = VisionSource(
        id=5,
        owner_id=1,
        name="img",
        scenario="gate",
        status="live",
        kind="file",
        uri=str(path),
        mime="image/jpeg",
        extra={"preview": "image"},
    )
    rule = DetectionRule(
        id=6,
        owner_id=1,
        name="yolo",
        source_id=5,
        algorithms=["no_helmet"],
        confidence=0.5,
        detector="small",
        small_model="yolo",
        enabled=True,
    )
    boxes, skipped, hint, _jpeg = detect_frame(src, [rule], 1, vlm_ok=False)
    assert boxes == []
    assert skipped is True
    assert "YOLO" in hint or "ultralytics" in hint or "权重" in hint


def test_session_rule_saves_model_choice(client, agents):
    jpeg = _jpeg_bytes()
    uploaded = client.post(
        "/api/vision/sources/upload",
        files=[("files", ("选模型.jpg", jpeg, "image/jpeg"))],
        data={"category": "other", "visibility": "private", "shared_user_ids": "[]"},
    ).json()
    src_id = uploaded["created"][0]["id"]
    session = client.post(
        f"/api/vision/sources/{src_id}/session-rule",
        json={"algorithms": ["no_helmet"], "detector": "vlm", "small_model": "demo", "vlm_model": "custom:vl"},
    ).json()
    assert session["small_model"] == "demo"
    assert session["vlm_model"] == "custom:vl"
    assert session["detector"] == "vlm"
    client.delete(f"/api/vision/sources/{src_id}")


def test_media_folder_and_batch_delete(client):
    folder = client.post("/api/vision/folders", json={"name": "现场一组"}).json()
    assert folder["name"] == "现场一组"
    jpeg = _jpeg_bytes()
    uploaded = client.post(
        "/api/vision/sources/upload",
        files=[
            ("files", ("a.jpg", jpeg, "image/jpeg")),
            ("files", ("b.jpg", jpeg, "image/jpeg")),
        ],
        data={"category": "other", "visibility": "private", "shared_user_ids": "[]", "folder_id": str(folder["id"])},
    ).json()
    ids = [item["id"] for item in uploaded["created"]]
    assert len(ids) == 2
    assert all(item["folder_id"] == folder["id"] for item in uploaded["created"])
    listed = client.get("/api/vision/sources?group=owner").json()
    assert any(f["id"] == folder["id"] for f in listed["folders"])
    moved = client.post("/api/vision/sources/move", json={"ids": [ids[0]], "folder_id": None}).json()
    assert moved["sources"][0]["folder_id"] is None
    deleted = client.post("/api/vision/sources/batch-delete", json={"ids": ids}).json()
    assert set(deleted["deleted"]) == set(ids)
    client.delete(f"/api/vision/folders/{folder['id']}")
    settings = client.put("/api/vision/model-settings", json={"small_model": "demo", "yolo_path": "", "vlm_model": ""}).json()
    assert settings["small_model"] == "demo"


def test_chat_upload_sorted_by_date_and_kind(client, tmp_path, monkeypatch):
    from youju.chatfiles import classify_ext

    assert classify_ext("现场.jpg")[0] == "images"
    assert classify_ext("说明.pdf")[0] == "documents"
    uploaded = client.post(
        "/api/chat/uploads",
        files=[("files", ("现场.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 8, "image/png"))],
    )
    assert uploaded.status_code == 200, uploaded.text
    item = uploaded.json()["uploads"][0]
    assert item["kind"] == "images"
    assert client.get(item["url"]).status_code == 200


def test_install_uploaded_model_config(client):
    import json

    from youju.db import SessionLocal
    from youju.models import User
    from youju.providers.registry import get_setting
    from youju.vision.install_upload import install_uploads

    weight = client.post("/api/chat/uploads", files=[("files", ("helmet.pt", b"pt-bytes", "application/octet-stream"))])
    assert weight.status_code == 200, weight.text
    config = json.dumps({"vlm_model": "", "small_model": "yolo:helmet.pt"}).encode()
    cfg = client.post("/api/chat/uploads", files=[("files", ("vision.json", config, "application/json"))])
    assert cfg.status_code == 200, cfg.text
    ids = [weight.json()["uploads"][0]["id"], cfg.json()["uploads"][0]["id"]]
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == "admin").one()
        notes = install_uploads(db, user, None, ids)
        db.commit()
        assert any("已安装权重" in note for note in notes)
        assert get_setting(db, "vision_small_model", "").startswith("yolo:")
    finally:
        db.close()


def test_agent_config_intent_and_model_bind(client, agents):
    from youju.agent.prompts import is_config_request

    assert is_config_request("帮我下载yolo的开放模型，我这边需要用模型进行图片检测")
    assert not is_config_request("安全带发放和检查怎么做")
    agent = agents["safety"]
    bound = client.patch(f"/api/agents/{agent['id']}/model", json={"model_ref": "local:extractive"}).json()
    assert bound["model_ref"] in (None, "")
    missing = client.patch(f"/api/agents/{agent['id']}/model", json={"model_ref": "no-such:model"})
    assert missing.status_code == 400


def test_scenario_for_algorithms_picks_matching_scene():
    from youju.vision.engine import scenario_for_algorithms

    assert scenario_for_algorithms(["no_belt", "no_hook"]) == "tower"
    assert scenario_for_algorithms(["no_helmet"]) == "gate"
    assert scenario_for_algorithms(["fire_smoke"]) == "hotwork"
    assert scenario_for_algorithms([]) == "gate"


def test_static_image_demo_boxes_no_alert_and_stop_keeps_boxes(client, agents):
    """静态图仅小模型：演示框可出现，但不落告警；停止后仍保留 boxes。"""
    jpeg = _jpeg_bytes()
    uploaded = client.post(
        "/api/vision/sources/upload",
        files=[("files", ("巡检现场.jpg", jpeg, "image/jpeg"))],
        data={"category": "other", "visibility": "private", "shared_user_ids": "[]"},
    ).json()
    src_id = uploaded["created"][0]["id"]
    assert uploaded["created"][0]["preview_kind"] == "image"

    session = client.post(
        f"/api/vision/sources/{src_id}/session-rule",
        json={"algorithms": ["no_helmet"], "detector": "small", "agent_id": agents["safety"]["id"], "auto_push": False},
    ).json()
    assert session["algorithms"] == ["no_helmet"]
    # 应用设置后场景应对齐到含安全帽的 gate
    src = client.get("/api/vision/sources").json()["sources"]
    me = next(s for s in src if s["id"] == src_id)
    assert me["scenario"] == "gate"

    started = client.post(f"/api/vision/sources/{src_id}/start").json()
    assert started["live"] is True
    boxes = []
    for _ in range(8):
        tick = client.post(f"/api/vision/sources/{src_id}/tick").json()
        boxes = tick.get("boxes") or []
        if boxes:
            break
    assert boxes and all(b.get("demo") for b in boxes)
    alerts = [a for a in client.get("/api/vision/alerts").json()["alerts"] if a["source_id"] == src_id]
    assert alerts == []

    stopped = client.post(f"/api/vision/sources/{src_id}/stop").json()
    assert stopped["live"] is False
    assert stopped["boxes"] == boxes

    batch = client.post("/api/vision/sources/batch-start", json={"ids": [src_id]}).json()
    assert batch["sources"] and batch["sources"][0]["live"]
    client.post("/api/vision/sources/batch-stop", json={"ids": [src_id]})
    client.delete(f"/api/vision/sources/{src_id}")


def test_detect_frame_defers_vlm_without_blocking_cache(tmp_path):
    from PIL import Image

    from youju.models import DetectionRule, VisionSource
    from youju.vision.engine import detect_frame, _vlm_cache

    path = tmp_path / "a.jpg"
    Image.new("RGB", (48, 32), color=(1, 2, 3)).save(path, format="JPEG")
    src = VisionSource(
        id=77,
        owner_id=1,
        name="img",
        scenario="gate",
        status="live",
        kind="file",
        uri=str(path),
        mime="image/jpeg",
        extra={"preview": "image"},
    )
    rule = DetectionRule(
        id=88,
        owner_id=1,
        name="vlm",
        source_id=77,
        algorithms=["no_helmet"],
        confidence=0.5,
        detector="vlm",
        enabled=True,
    )
    _vlm_cache[77] = (
        1,
        [{"id": "no_helmet", "name": "未戴安全帽", "box": [0.1, 0.1, 0.2, 0.2], "confidence": 0.9, "level": "medium", "detector": "vlm", "rule_id": 88, "from_image": True, "reason": "可见"}],
        b"\xff\xd8\xff",
        "",
    )
    hits, skipped, _hint, jpeg = detect_frame(src, [rule], 3, vlm_ok=True, allow_vlm_call=False)
    assert jpeg
    assert any(h.get("from_image") for h in hits)
    assert skipped is False or hits
    _vlm_cache.pop(77, None)
