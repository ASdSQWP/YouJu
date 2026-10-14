"""有据压测：对话、检索、视觉拉流/出帧/告警。

默认起一个临时库，不碰你平时用的 data/youju.db。

  python scripts/stress.py
  python scripts/stress.py --quick
  python scripts/stress.py --url http://127.0.0.1:8000 --user admin --password admin123
"""

from __future__ import annotations

import argparse
import os
import random
import socket
import statistics
import sys
import tempfile
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import logging

import httpx

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

QUESTIONS = [
    "安全带没扣好可以继续干活吗？",
    "画面里发现烟火先做什么？",
    "CPU 使用率一直很高怎么排查？",
    "工作满一年有几天年假？",
    "签收后几天内可以无理由退货？",
]
SEARCHES = [
    "安全带",
    "烟火监护人",
    "磁盘空间",
    "年假天数",
    "退换货",
]
VIDEO_NAMES = ["杆塔3", "动火区", "工地大门", "限界监测"]


class Stats:
    def __init__(self):
        self.lock = threading.Lock()
        self.lat = defaultdict(list)
        self.ok = defaultdict(int)
        self.err = defaultdict(list)
        self.codes = defaultdict(lambda: defaultdict(int))

    def add(self, name: str, ms: float, status: int, error: str = ""):
        with self.lock:
            self.lat[name].append(ms)
            self.codes[name][status] += 1
            if 200 <= status < 400 and not error:
                self.ok[name] += 1
            else:
                self.err[name].append(error or f"HTTP {status}")

    def snapshot(self) -> list[tuple]:
        rows = []
        with self.lock:
            names = sorted(set(self.lat) | set(self.ok) | set(self.err))
            for name in names:
                samples = sorted(self.lat.get(name, []))
                n = len(samples)
                fails = self.err.get(name, [])
                p50 = p95 = p99 = 0
                if samples:
                    p50 = samples[min(len(samples) - 1, int(0.50 * n))]
                    p95 = samples[min(len(samples) - 1, int(0.95 * n))]
                    p99 = samples[min(len(samples) - 1, int(0.99 * n))]
                rows.append((name, n, self.ok.get(name, 0), len(fails), p50, p95, p99, fails[:3]))
        return rows


def pct(samples, q):
    if not samples:
        return 0
    s = sorted(samples)
    return s[min(len(s) - 1, int(q * len(s)))]


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def timed(client: httpx.Client, stats: Stats, name: str, method: str, path: str, **kw):
    t0 = time.perf_counter()
    status = 0
    err = ""
    try:
        resp = client.request(method, path, **kw)
        status = resp.status_code
        if status >= 400:
            try:
                err = resp.json().get("detail") or resp.text[:180]
            except Exception:
                err = resp.text[:180]
        if name.startswith("vision.frame") and status == 200 and resp.content[:8] != b"\x89PNG\r\n\x1a\n":
            err = "frame 不是 PNG"
            status = 599
        if name.startswith("chat") and status == 200 and "data:" not in resp.text:
            err = "chat 没有 SSE"
            status = 599
    except Exception as exc:  # noqa: BLE001
        err = f"{exc.__class__.__name__}: {exc}"
        status = 0
    stats.add(name, (time.perf_counter() - t0) * 1000, status, err)
    return status, err


def start_embedded(port: int) -> tuple[threading.Thread, object, str]:
    data = tempfile.mkdtemp(prefix="youju-stress-")
    os.environ["YOUJU_DATA_DIR"] = data
    os.environ["YOUJU_OPEN_BROWSER"] = "0"
    os.environ.pop("YOUJU_DATABASE_URL", None)
    os.environ.pop("YOUJU_ADMIN_PASSWORD", None)

    import uvicorn
    from youju.main import app

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True, name="youju-stress-server")
    thread.start()
    deadline = time.time() + 20
    url = f"http://127.0.0.1:{port}"
    while time.time() < deadline:
        try:
            resp = httpx.get(f"{url}/api/health", timeout=1, trust_env=False)
            if resp.status_code == 200:
                return thread, server, url
        except Exception:
            time.sleep(0.1)
        else:
            time.sleep(0.1)
    raise RuntimeError("临时服务没有起来")


def login(client: httpx.Client, user: str, password: str) -> None:
    status = client.get("/api/auth/status").json()
    if not status.get("initialized"):
        resp = client.post(
            "/api/auth/setup",
            json={"username": user, "password": password, "display_name": "压测", "seed_demo": True},
            timeout=120,
        )
        resp.raise_for_status()
        return
    if status.get("user"):
        return
    resp = client.post("/api/auth/login", json={"username": user, "password": password})
    resp.raise_for_status()


def ids(client: httpx.Client) -> dict:
    agents = {a["template"]: a for a in client.get("/api/agents").json()["agents"]}
    kbs = {kb["name"]: kb for kb in client.get("/api/kbs").json()["kbs"]}
    sources = client.get("/api/vision/sources").json()["sources"]
    return {"agents": agents, "kbs": kbs, "sources": sources}


def burst(pool: ThreadPoolExecutor, n: int, fn):
    futs = [pool.submit(fn, i) for i in range(n)]
    for fut in as_completed(futs):
        fut.result()


def run_phases(client: httpx.Client, stats: Stats, quick: bool) -> None:
    ctx = ids(client)
    safety = ctx["agents"]["safety"]["id"]
    hr = ctx["agents"]["hr"]["id"]
    kb_safety = ctx["kbs"]["安全生产规程"]["id"]
    workers = 4 if quick else 12
    reads = 24 if quick else 80
    searches = 12 if quick else 40
    chats = 6 if quick else 16
    frames = 24 if quick else 80
    mixed = 8 if quick else 20

    print("1/6 冒烟")
    timed(client, stats, "health", "GET", "/api/health")
    timed(client, stats, "agents", "GET", "/api/agents")
    timed(client, stats, "kbs", "GET", "/api/kbs")
    timed(client, stats, "search", "POST", f"/api/kbs/{kb_safety}/search", json={"query": "安全带没扣好"})
    conv = client.post("/api/conversations", json={"agent_id": safety}).json()
    timed(
        client,
        stats,
        "chat",
        "POST",
        f"/api/conversations/{conv['id']}/chat",
        json={"content": "安全带没扣好可以继续干活吗？", "model": "local:extractive"},
        timeout=60,
    )
    timed(client, stats, "vision.sources", "GET", "/api/vision/sources")
    timed(client, stats, "vision.catalog", "GET", "/api/vision/catalog")
    timed(client, stats, "vision.compose", "POST", "/api/vision/rules/compose", json={"instruction": "工地大门检测未戴安全帽", "apply": False})
    for name in VIDEO_NAMES:
        timed(client, stats, "vision.pull", "POST", "/api/vision/pull", json={"name": name})
    src_id = ctx["sources"][0]["id"]
    timed(client, stats, "vision.frame", "GET", f"/api/vision/sources/{src_id}/frame")
    timed(client, stats, "vision.preview", "GET", f"/api/vision/sources/{src_id}/preview", params={"advance": "1"})
    timed(client, stats, "vision.alerts", "GET", "/api/vision/alerts")
    timed(client, stats, "events", "GET", "/api/events")

    print(f"2/6 并发读 {workers}×{reads}")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        paths = ["/api/health", "/api/agents", "/api/kbs", "/api/conversations", "/api/events", "/api/vision/sources", "/api/vision/rules", "/api/vision/alerts"]

        def read_one(i):
            path = paths[i % len(paths)]
            timed(client, stats, f"read{path}", "GET", path)

        burst(pool, reads, read_one)

    print(f"3/6 并发检索 {workers}×{searches}")
    kb_ids = [kb["id"] for kb in ctx["kbs"].values()]
    with ThreadPoolExecutor(max_workers=workers) as pool:

        def search_one(i):
            timed(
                client,
                stats,
                "search",
                "POST",
                f"/api/kbs/{kb_ids[i % len(kb_ids)]}/search",
                json={"query": SEARCHES[i % len(SEARCHES)]},
            )

        burst(pool, searches, search_one)

    print(f"4/6 并发对话 {max(2, workers // 3)}×{chats}")
    with ThreadPoolExecutor(max_workers=max(2, workers // 3)) as pool:

        def chat_one(i):
            agent = safety if i % 2 == 0 else hr
            q = QUESTIONS[i % len(QUESTIONS)]
            created = client.post("/api/conversations", json={"agent_id": agent})
            if created.status_code >= 400:
                stats.add("chat", 0, created.status_code, created.text[:180])
                return
            timed(
                client,
                stats,
                "chat",
                "POST",
                f"/api/conversations/{created.json()['id']}/chat",
                json={"content": q, "model": "local:extractive"},
                timeout=60,
            )

        burst(pool, chats, chat_one)

    print(f"5/6 视觉出帧 {workers}×{frames}")
    sources = client.get("/api/vision/sources").json()["sources"]
    live_ids = [s["id"] for s in sources]
    with ThreadPoolExecutor(max_workers=workers) as pool:

        def vision_one(i):
            sid = live_ids[i % len(live_ids)]
            kind = i % 4
            if kind == 0:
                timed(client, stats, "vision.frame", "GET", f"/api/vision/sources/{sid}/frame")
            elif kind == 1:
                timed(client, stats, "vision.preview", "GET", f"/api/vision/sources/{sid}/preview", params={"advance": "1"})
            elif kind == 2:
                timed(client, stats, "vision.alerts", "GET", "/api/vision/alerts")
            else:
                timed(client, stats, "vision.tick", "POST", f"/api/vision/sources/{sid}/tick")

        burst(pool, frames, vision_one)

    print(f"6/6 混合 {workers}×{mixed}")
    with ThreadPoolExecutor(max_workers=workers) as pool:

        def mix_one(i):
            roll = i % 7
            if roll == 0:
                timed(client, stats, "health", "GET", "/api/health")
            elif roll == 1:
                timed(client, stats, "agents", "GET", "/api/agents")
            elif roll == 2:
                timed(client, stats, "search", "POST", f"/api/kbs/{kb_safety}/search", json={"query": random.choice(SEARCHES)})
            elif roll == 3:
                timed(client, stats, "vision.frame", "GET", f"/api/vision/sources/{live_ids[i % len(live_ids)]}/frame")
            elif roll == 4:
                timed(client, stats, "vision.preview", "GET", f"/api/vision/sources/{live_ids[i % len(live_ids)]}/preview", params={"advance": "1"})
            elif roll == 5:
                timed(client, stats, "events", "GET", "/api/events")
            else:
                timed(client, stats, "vision.alerts", "GET", "/api/vision/alerts")

        burst(pool, mixed, mix_one)

    for name in VIDEO_NAMES:
        src = next((s for s in client.get("/api/vision/sources").json()["sources"] if name in s["name"]), None)
        if src:
            timed(client, stats, "vision.stop", "POST", f"/api/vision/sources/{src['id']}/stop")


def print_report(stats: Stats) -> int:
    rows = stats.snapshot()
    print()
    print(f"{'接口':<28} {'次数':>6} {'成功':>6} {'失败':>6} {'p50':>8} {'p95':>8} {'p99':>8}")
    print("-" * 80)
    bad = 0
    for name, n, ok, fails, p50, p95, p99, samples in rows:
        print(f"{name:<28} {n:>6} {ok:>6} {fails:>6} {p50:>7.0f}ms {p95:>7.0f}ms {p99:>7.0f}ms")
        if fails:
            bad += fails
            for item in samples:
                print(f"    ! {item}")
    total = sum(n for _, n, *_ in rows)
    ok = sum(o for _, _, o, *_ in rows)
    print("-" * 80)
    print(f"合计 {total} 次，成功 {ok}，失败 {bad}")
    return bad


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    parser = argparse.ArgumentParser(description="有据接口压测")
    parser.add_argument("--url", default="", help="已启动的服务地址；不填则起临时服务")
    parser.add_argument("--user", default="admin")
    parser.add_argument("--password", default="stress-pass-123")
    parser.add_argument("--quick", action="store_true", help="缩短轮次，适合改完代码冒烟")
    args = parser.parse_args()

    server = None
    url = args.url.rstrip("/")
    if not url:
        port = free_port()
        print(f"启动临时服务 http://127.0.0.1:{port} （独立数据库，不影响 data/）")
        _, server, url = start_embedded(port)
    else:
        print(f"压测已有服务 {url}")

    stats = Stats()
    try:
        with httpx.Client(base_url=url, timeout=30.0, follow_redirects=True, trust_env=False) as client:
            login(client, args.user, args.password)
            print("登录完成，开始压测")
            run_phases(client, stats, args.quick)
    finally:
        if server is not None:
            server.should_exit = True

    bad = print_report(stats)
    if bad:
        print("\n压测发现失败请求，需要处理。")
        return 1
    print("\n压测通过，没有失败请求。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
