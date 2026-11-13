"""仿真排障闭环：权限、非结论、审核追溯和评测分母。"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from youju.diag_eval import load_cases, run_labeled
from youju.main import app


def _login(username: str, password: str = "member-pass-99182") -> TestClient:
    client = TestClient(app)
    admin = TestClient(app)
    assert admin.post("/api/auth/login", json={"username": "admin", "password": "admin-pass-123"}).status_code == 200
    made = admin.post("/api/users", json={"username": username, "password": password, "display_name": username})
    assert made.status_code in (200, 409), made.text
    assert client.post("/api/auth/login", json={"username": username, "password": password}).status_code == 200
    return client


def test_fault_loop_permissions_and_trace(client):
    member = _login("fault-member")
    other = _login("fault-other")
    imported = member.post("/api/faults/samples")
    assert imported.status_code == 200, imported.text
    cases = {row["title"]: row for row in imported.json()["cases"]}
    assert cases["仿真：视频断流"]["label"] == "仿真样例"
    stream = cases["仿真：视频断流"]
    weak = cases["仿真：依据不足"]
    assert other.get(f"/api/faults/{stream['id']}").status_code == 404
    assert all(row["id"] != stream["id"] for row in other.get("/api/faults").json()["cases"])

    diagnosed = member.post(f"/api/faults/{stream['id']}/diagnose", json={"use_model": False})
    assert diagnosed.status_code == 200, diagnosed.text
    body = diagnosed.json()
    diag = body["diagnosis"]
    assert diag["status"] == "concluded", diag
    assert diag["banner"] == ""
    assert diag["evidence"]
    assert diag["evidence"][0]["chunk_id"]
    assert "视频断流处置.md" in diag["evidence"][0]["filename"]
    assert diag["evidence"][0]["section"]
    assert "拉流" in diag["evidence"][0]["text"]
    assert diag["mode"] == "offline"
    change = body["change"]
    assert change["status"] == "pending"
    assert "不会修改文件" in change["impact"]
    assert change["checks"]

    weak_body = member.post(f"/api/faults/{weak['id']}/diagnose", json={}).json()
    assert weak_body["diagnosis"]["status"] == "inconclusive"
    assert weak_body["diagnosis"]["banner"] == "非结论"
    assert weak_body["diagnosis"]["evidence"] == []
    assert "不是结论" in weak_body["change"]["suggestion"]
    export_weak = member.get(f"/api/faults/{weak['id']}/export").text
    assert "非结论" in export_weak
    assert "回归通过" not in export_weak

    blocked = member.post(
        f"/api/faults/{stream['id']}/changes/{change['id']}/review",
        json={"action": "ship"},
    )
    assert blocked.status_code == 422
    reviewed = member.post(
        f"/api/faults/{stream['id']}/changes/{change['id']}/review",
        json={"action": "approve", "note": "只在本地确认"},
    ).json()
    assert reviewed["status"] == "approved"
    assert reviewed["reviewer_name"]
    assert reviewed["reviewed_at"]
    again = member.post(
        f"/api/faults/{stream['id']}/changes/{change['id']}/review",
        json={"action": "approve"},
    )
    assert again.status_code == 409

    edited = member.patch(
        f"/api/faults/{stream['id']}/changes/{change['id']}",
        json={"suggestion": "只复查仿真日志，不操作设备。"},
    ).json()
    assert edited["status"] == "pending"
    assert edited["version"] == change["version"] + 1
    assert edited["reviewer_id"] is None
    trace = member.get(f"/api/faults/{stream['id']}").json()
    versions = {item["version"]: item["status"] for item in trace["changes"]}
    assert versions[change["version"]] == "approved"
    assert versions[edited["version"]] == "pending"

    early = member.get(f"/api/faults/{stream['id']}/export").text
    assert "回归通过" not in early
    bad = member.post(
        f"/api/faults/{stream['id']}/regressions",
        json={"change_id": edited["id"], "item": edited["checks"][0], "result": "passed"},
    )
    assert bad.status_code == 422
    for item in edited["checks"]:
        saved = member.post(
            f"/api/faults/{stream['id']}/regressions",
            json={"change_id": edited["id"], "item": item, "result": "untested", "note": "还没看"},
        ).json()
        assert saved["summary"]["passed"] is False
    mid = member.get(f"/api/faults/{stream['id']}/export").text
    assert "回归通过" not in mid
    assert "人工录入" in mid
    for item in edited["checks"]:
        saved = member.post(
            f"/api/faults/{stream['id']}/regressions",
            json={"change_id": edited["id"], "item": item, "result": "pass", "note": "人工看过摘录"},
        ).json()
    assert saved["summary"]["verdict"] == "回归通过"
    final = member.get(f"/api/faults/{stream['id']}/export").text
    assert "回归通过" in final
    assert "仿真" in final
    assert "人工录入" in final


def test_revoked_citation_hides_text(client):
    member = _login("fault-revoke")
    kb = client.post("/api/kbs", json={"name": "暂时团队规程", "visibility": "team"}).json()
    secret = "撤销前可见的规程原句：紫藤闸门必须先断电。"
    uploaded = client.post(
        f"/api/kbs/{kb['id']}/documents",
        files=[("files", ("临时.md", f"# 临时\n\n## 处置\n\n{secret}\n".encode(), "text/markdown"))],
    )
    assert uploaded.status_code == 200, uploaded.text
    created = member.post(
        "/api/faults",
        json={
            "title": "会撤权的记录",
            "category": "other",
            "phenomenon": "紫藤闸门必须先断电才能检修",
            "version": "demo",
            "log_text": "仿真日志 紫藤闸门",
            "kb_ids": [kb["id"]],
        },
    )
    assert created.status_code == 200, created.text
    case_id = created.json()["id"]
    diag = member.post(f"/api/faults/{case_id}/diagnose", json={}).json()["diagnosis"]
    assert secret in json.dumps(diag, ensure_ascii=False)
    assert client.patch(f"/api/kbs/{kb['id']}", json={"visibility": "private"}).status_code == 200
    hidden = member.get(f"/api/faults/{case_id}").json()
    blob = json.dumps(hidden, ensure_ascii=False)
    assert secret not in blob
    assert "来源已不可见" in blob
    exported = member.get(f"/api/faults/{case_id}/export").text
    assert secret not in exported
    admin_view = client.get(f"/api/faults/{case_id}").json()
    assert secret in json.dumps(admin_view, ensure_ascii=False)
    client.delete(f"/api/kbs/{kb['id']}")


def test_optional_model_is_speculation_only(client, monkeypatch):
    member = _login("fault-model")
    case = next(row for row in member.post("/api/faults/samples").json()["cases"] if row["category"] == "stream")

    class Fake:
        async def stream(self, model, messages, tools=None, temperature=None, tool_choice=None, max_tokens=None):
            from youju.providers.base import StreamPart

            yield StreamPart(kind="text", text="也许是网线松了，但不要执行。")
            yield StreamPart(kind="usage", usage={"input": 3, "output": 4})

    info = type("Info", (), {"model": "mock", "ref": "fake:mock"})()
    monkeypatch.setattr("youju.faults.resolve_chat", lambda db, *refs: (Fake(), info))
    body = member.post(f"/api/faults/{case['id']}/diagnose", json={"use_model": True}).json()
    assert body["diagnosis"]["mode"] == "model"
    assert "网线" in body["diagnosis"]["speculation"]
    assert all("网线" not in (item.get("text") or "") for item in body["diagnosis"]["evidence"])
    assert "不会修改文件" in body["change"]["impact"]


def test_diag_baseline_denominators():
    member = _login("fault-baseline")
    imported = member.post("/api/faults/samples").json()
    kb_id = imported["cases"][0]["kb_ids"][0]
    report = run_labeled(kb_id)
    metrics = report["metrics"]
    cases = load_cases()
    assert metrics["file_hit"]["denom"] == sum(1 for row in cases if row.get("expect_file"))
    assert metrics["evidence_gate"]["denom"] == sum(1 for row in cases if "expect_evidence" in row)
    assert metrics["refuse"]["denom"] == sum(1 for row in cases if "expect_refuse" in row)
    assert metrics["citation_lexical"]["denom"] <= metrics["evidence_gate"]["denom"]
    assert "不是事实核验" in metrics["note"]
    by_id = {row["id"]: row for row in report["rows"]}
    assert by_id["orig-stream"]["file_ok"] is True
    assert by_id["orig-stream"]["fragment_ok"] is True
    assert by_id["none-menu"]["evidence"] is False
    assert by_id["none-menu"]["refused"] is True
    assert by_id["wrong-frag"]["top_file"] != "对讲异常处置.md"
    assert by_id["neg-talk"]["constraint_ok"] is True
    assert imported["cases"]
