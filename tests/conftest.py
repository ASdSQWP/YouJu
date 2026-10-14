import os
import tempfile

os.environ["YOUJU_DATA_DIR"] = tempfile.mkdtemp(prefix="youju-test-")
os.environ.pop("YOUJU_DATABASE_URL", None)
os.environ.pop("YOUJU_ADMIN_PASSWORD", None)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from youju.main import app  # noqa: E402

ADMIN = {"username": "admin", "password": "admin-pass-123"}


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        resp = c.post("/api/auth/setup", json={**ADMIN, "display_name": "管理员", "seed_demo": True})
        assert resp.status_code == 200, resp.text
        yield c


@pytest.fixture(scope="session")
def agents(client):
    return {a["template"]: a for a in client.get("/api/agents").json()["agents"]}


@pytest.fixture(scope="session")
def kbs(client):
    return {kb["name"]: kb for kb in client.get("/api/kbs").json()["kbs"]}
