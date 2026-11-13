"""三项权限边界的回归：视觉路径、全局视觉配置、私有知识库。

使用测试夹具里的合成数据和临时上传目录，不连接外部模型。
"""

from __future__ import annotations

import io
import json

from fastapi.testclient import TestClient
from PIL import Image

from helpers import ask
from youju.config import settings
from youju.main import app
from youju.providers.registry import get_setting
from youju.safepath import contained_file, vision_upload_root

PRIVATE = "紫藤工位的保险柜密码写在封条内层"
TEAM = "青竹班组的备用钥匙放在前台抽屉"
OUTSIDE = "ACL-OUTSIDE-SECRET-22001"


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (24, 16), color=(12, 40, 90)).save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def _member(username: str = "acl-member") -> TestClient:
    client = TestClient(app)
    created = client.post(
        "/api/auth/login",
        json={"username": username, "password": "member-pass-99182"},
    )
    if created.status_code != 200:
        admin = TestClient(app)
        admin.post(
            "/api/auth/login",
            json={"username": "admin", "password": "admin-pass-123"},
        )
        made = admin.post(
            "/api/users",
            json={"username": username, "password": "member-pass-99182", "display_name": "边界成员"},
        )
        assert made.status_code in (200, 409), made.text
        assert client.post(
            "/api/auth/login",
            json={"username": username, "password": "member-pass-99182"},
        ).status_code == 200
    return client


def _ready(client: TestClient, kb_id: int) -> None:
    docs = client.get(f"/api/kbs/{kb_id}/documents").json()["documents"]
    assert docs and all(doc["status"] == "ready" for doc in docs), docs


def test_contained_file_rejects_escape(tmp_path):
    root = tmp_path / "uploads"
    nested = root / "vision" / "3"
    nested.mkdir(parents=True)
    good = nested / "ok.jpg"
    good.write_bytes(b"jpeg")
    outside = tmp_path / "secret.txt"
    outside.write_text(OUTSIDE, encoding="utf-8")
    link = nested / "link.jpg"
    link.symlink_to(outside)
    assert contained_file(root, good) == good.resolve()
    assert contained_file(root, link) is None
    assert contained_file(root, outside) is None
    assert contained_file(root, nested / ".." / ".." / "secret.txt") is None
    assert contained_file(root, "secret.txt") is None


def test_own_upload_preview_survives_path_jail(client):
    jpeg = _jpeg()
    uploaded = client.post(
        "/api/vision/sources/upload",
        files=[("files", ("acl-preview.jpg", jpeg, "image/jpeg"))],
        data={"category": "other", "visibility": "private", "shared_user_ids": "[]"},
    )
    assert uploaded.status_code == 200, uploaded.text
    src = uploaded.json()["created"][0]
    assert src["uri"] == ""
    media = client.get(f"/api/vision/sources/{src['id']}/media")
    assert media.status_code == 200
    assert media.content == jpeg
    assert media.headers["content-type"].startswith("image/jpeg")
    frame = client.get(f"/api/vision/sources/{src['id']}/frame")
    assert frame.status_code == 200 and len(frame.content) > 50
    owner_id = src["owner_id"]
    stored = next(vision_upload_root(owner_id).iterdir())
    again = client.post(
        "/api/vision/sources",
        json={"name": "再次引用", "kind": "file", "uri": str(stored), "visibility": "private"},
    )
    assert again.status_code == 200, again.text
    assert client.get(f"/api/vision/sources/{again.json()['id']}/media").content == jpeg
    client.delete(f"/api/vision/sources/{src['id']}")
    client.delete(f"/api/vision/sources/{again.json()['id']}")


def test_vision_file_read_rejects_traversal_and_symlink(client):
    member = _member()
    outside = settings.data_dir / "acl-outside.txt"
    outside.write_text(OUTSIDE, encoding="utf-8")
    for uri in (str(outside), "youju.db", str(settings.data_dir / "youju.db")):
        denied = member.post("/api/vision/sources", json={"name": "越界", "kind": "file", "uri": uri})
        assert denied.status_code == 400, denied.text
        assert OUTSIDE.encode() not in denied.content

    jpeg = _jpeg()
    uploaded = member.post(
        "/api/vision/sources/upload",
        files=[("files", ("mine.jpg", jpeg, "image/jpeg"))],
        data={"category": "other", "visibility": "private", "shared_user_ids": "[]"},
    )
    src = uploaded.json()["created"][0]
    folder = vision_upload_root(src["owner_id"])
    stored = next(path for path in folder.iterdir() if path.suffix == ".jpg")
    stored.unlink()
    stored.symlink_to(outside)
    media = member.get(f"/api/vision/sources/{src['id']}/media")
    frame = member.get(f"/api/vision/sources/{src['id']}/frame")
    assert media.status_code == 404
    assert OUTSIDE.encode() not in media.content
    assert OUTSIDE.encode() not in frame.content

    admin_upload = client.post(
        "/api/vision/sources/upload",
        files=[("files", ("admin.jpg", jpeg, "image/jpeg"))],
        data={"category": "other", "visibility": "private", "shared_user_ids": "[]"},
    )
    admin_src = admin_upload.json()["created"][0]
    admin_file = next(vision_upload_root(admin_src["owner_id"]).glob("*.jpg"))
    stolen = member.post("/api/vision/sources", json={"name": "别人的文件", "kind": "file", "uri": str(admin_file)})
    assert stolen.status_code == 400

    from youju.db import SessionLocal
    from youju.models import DetectionAlert

    db = SessionLocal()
    try:
        alert = DetectionAlert(
            source_id=src["id"],
            title="越界截图",
            snapshot_path=str(outside),
            findings=[],
            boxes=[],
        )
        db.add(alert)
        db.commit()
        alert_id = alert.id
    finally:
        db.close()
    snap = member.get(f"/api/vision/alerts/{alert_id}/snapshot")
    assert snap.status_code == 404
    assert OUTSIDE.encode() not in snap.content
    client.delete(f"/api/vision/sources/{admin_src['id']}")


def test_global_vision_settings_require_admin(client):
    member = _member("acl-settings-member")
    before = {
        "small_model": get_setting_value("vision_small_model"),
        "yolo_path": get_setting_value("vision_yolo_path"),
        "vlm_model": get_setting_value("vision_vlm_model"),
    }
    denied = member.put(
        "/api/vision/model-settings",
        json={"small_model": "yolo:pwned", "yolo_path": "/tmp/pwned.pt", "vlm_model": "stolen"},
    )
    assert denied.status_code == 403
    assert member.get("/api/vision/model-settings").status_code == 403
    assert member.post("/api/vision/model-settings/assist", json={"instruction": "改成 yolo"}).status_code == 403

    weight = member.post("/api/chat/uploads", files=[("files", ("helmet.pt", b"not-a-real-weight", "application/octet-stream"))])
    config = json.dumps({"small_model": "yolo:from-member", "yolo_path": "/tmp/member.pt", "vlm_model": "member-vlm"}).encode()
    cfg = member.post("/api/chat/uploads", files=[("files", ("vision.json", config, "application/json"))])
    assert weight.status_code == 200 and cfg.status_code == 200
    agents = member.get("/api/agents").json()["agents"]
    agent_id = agents[0]["id"]
    conv = member.post("/api/conversations", json={"agent_id": agent_id}).json()
    sent = member.post(
        f"/api/conversations/{conv['id']}/chat",
        json={
            "content": "请安装这个视觉配置",
            "model": "local:extractive",
            "attachment_ids": [weight.json()["uploads"][0]["id"], cfg.json()["uploads"][0]["id"]],
        },
    )
    assert sent.status_code == 200, sent.text
    assert get_setting_value("vision_small_model") == before["small_model"]
    assert get_setting_value("vision_yolo_path") == before["yolo_path"]
    assert get_setting_value("vision_vlm_model") == before["vlm_model"]

    saved = client.put(
        "/api/vision/model-settings",
        json={"small_model": "demo", "yolo_path": "", "vlm_model": ""},
    )
    assert saved.status_code == 200
    assert saved.json()["small_model"] == "demo"
    catalog = member.get("/api/vision/catalog").json()
    assert catalog["defaults"]["yolo_path"] == ""
    assert all("path" not in item for item in catalog["small_models"])


def test_private_kb_is_not_implicitly_shared(client):
    member = _member("acl-kb-member")
    private = client.post("/api/kbs", json={"name": "ACL私有库", "visibility": "private"}).json()
    team = client.post("/api/kbs", json={"name": "ACL团队库", "visibility": "team"}).json()
    client.post(
        f"/api/kbs/{private['id']}/documents",
        files=[("files", ("私有.md", f"{PRIVATE}。".encode(), "text/markdown"))],
    )
    client.post(
        f"/api/kbs/{team['id']}/documents",
        files=[("files", ("团队.md", f"{TEAM}。".encode(), "text/markdown"))],
    )
    _ready(client, private["id"])
    _ready(client, team["id"])
    agent = client.post(
        "/api/agents",
        json={
            "name": "ACL团队助手",
            "kb_ids": [private["id"], team["id"]],
            "visibility": "team",
            "strategy": "rag",
            "strict": True,
            "allow_events": True,
        },
    ).json()

    owner_answer = ask(client, agent["id"], "紫藤工位的保险柜密码写在哪里？")[2]
    assert PRIVATE in owner_answer["content"]
    member_detail = member.get(f"/api/agents/{agent['id']}").json()
    assert private["id"] not in member_detail["kb_ids"]
    assert all(item["name"] != "ACL私有库" for item in member_detail["kbs"])
    assert team["id"] in member_detail["kb_ids"]
    listed = member.get("/api/agents").json()["agents"]
    listed_agent = next(item for item in listed if item["id"] == agent["id"])
    assert "ACL私有库" not in json.dumps(listed_agent, ensure_ascii=False)

    leaked = ask(member, agent["id"], "紫藤工位的保险柜密码写在哪里？")[2]
    blob = json.dumps(leaked, ensure_ascii=False)
    assert PRIVATE not in blob
    assert all(source.get("filename") != "私有.md" for source in leaked.get("sources") or [])
    shared = ask(member, agent["id"], "青竹班组的备用钥匙放在哪里？")[2]
    assert TEAM in shared["content"]

    assert member.get(f"/api/kbs/{private['id']}").status_code == 404
    assert member.post(f"/api/kbs/{private['id']}/search", json={"query": PRIVATE}).status_code == 404
    assert member.post(f"/api/kbs/{private['id']}/eval/run", json={"top_k": 3}).status_code == 404

    key = member.post("/api/apikeys", json={"name": "acl"}).json()["key"]
    headers = {"Authorization": f"Bearer {key}"}
    completion = member.post(
        "/v1/chat/completions",
        headers=headers,
        json={"model": "ACL团队助手", "messages": [{"role": "user", "content": "紫藤工位的保险柜密码写在哪里？"}]},
    ).json()
    assert PRIVATE not in json.dumps(completion, ensure_ascii=False)

    def rpc(method, params=None, msg_id=1):
        body = {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}}
        return member.post("/mcp", headers=headers, json=body).json()

    names = rpc("tools/call", {"name": "list_knowledge_bases", "arguments": {}})["result"]["content"][0]["text"]
    assert "ACL私有库" not in names
    assert "ACL团队库" in names
    searched = rpc(
        "tools/call",
        {"name": "search_knowledge", "arguments": {"query": "紫藤工位保险柜", "knowledge_base": "ACL私有库"}},
    )["result"]
    assert searched["isError"] or PRIVATE not in searched["content"][0]["text"]
    asked = rpc(
        "tools/call",
        {"name": "ask_agent", "arguments": {"agent": "ACL团队助手", "question": "紫藤工位的保险柜密码写在哪里？"}},
    )["result"]["content"][0]["text"]
    assert PRIVATE not in asked

    pushed = member.post(
        f"/api/v1/agents/{agent['id']}/events",
        headers=headers,
        json={"title": "核对现场记号", "level": "low", "source": "acl", "data": {"note": "只是一条合成记号"}},
    )
    assert pushed.status_code == 202, pushed.text
    event = member.get(f"/api/events/{pushed.json()['id']}").json()
    assert PRIVATE not in (event.get("report") or "")
    assert all("私有.md" not in json.dumps(source, ensure_ascii=False) for source in event.get("sources") or [])

    # 显式改为私有后，成员不能继续命中原先团队库里的句子
    assert client.patch(f"/api/kbs/{team['id']}", json={"visibility": "private"}).status_code == 200
    revoked = ask(member, agent["id"], "青竹班组的备用钥匙放在哪里？")[2]
    assert TEAM not in json.dumps(revoked, ensure_ascii=False)
    after = member.get(f"/api/agents/{agent['id']}").json()
    assert team["id"] not in after["kb_ids"]
    owner_still = ask(client, agent["id"], "紫藤工位的保险柜密码写在哪里？")[2]
    assert PRIVATE in owner_still["content"]

    client.delete(f"/api/agents/{agent['id']}")
    client.delete(f"/api/kbs/{private['id']}")
    client.delete(f"/api/kbs/{team['id']}")


def get_setting_value(key: str):
    from youju.db import SessionLocal

    db = SessionLocal()
    try:
        return get_setting(db, key)
    finally:
        db.close()
