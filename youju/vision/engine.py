"""拉流会话和逐帧检测。演示点位用场景适配器；真实模型可替换 detect_frame。"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from youju.db import SessionLocal, utcnow
from youju.models import Agent, DetectionAlert, DetectionRule, Event, VisionSource
from youju.vision.catalog import SCENES, algo_level, algo_name
from youju.vision.render import draw_scene

log = logging.getLogger("youju.vision")
CYCLE = 24


def visible_objects(scenario: str, tick: int) -> list[dict]:
    scene = SCENES.get(scenario) or SCENES["tower"]
    phase = tick % CYCLE
    found = []
    for obj in scene["objects"]:
        start, end = obj["phase"]
        if start <= phase <= end:
            found.append({**obj, "name": algo_name(obj["id"]), "level": algo_level(obj["id"])})
    return found


def match_rule(rule: DetectionRule, source: VisionSource, objects: list[dict]) -> list[dict]:
    if not rule.enabled:
        return []
    if rule.source_id and rule.source_id != source.id:
        return []
    wanted = set(rule.algorithms or [])
    hits = []
    for obj in objects:
        if obj["id"] not in wanted:
            continue
        if float(obj["confidence"]) < float(rule.confidence or 0):
            continue
        hits.append(
            {
                "id": obj["id"],
                "name": obj["name"],
                "box": list(obj["box"]),
                "confidence": float(obj["confidence"]),
                "level": rule.level or obj["level"],
                "detector": rule.detector,
                "rule_id": rule.id,
            }
        )
    return hits


def detect_frame(source: VisionSource, rules: list[DetectionRule], tick: int) -> list[dict]:
    objects = visible_objects(source.scenario or "tower", tick)
    hits: list[dict] = []
    for rule in rules:
        hits.extend(match_rule(rule, source, objects))
    return hits


_frame_lock = threading.Lock()
_frame_cache: dict[tuple[int, int], bytes] = {}


def render_source(source: VisionSource, tick: int, boxes: list[dict] | None = None) -> bytes:
    key = (source.id, int(tick))
    with _frame_lock:
        hit = _frame_cache.get(key)
        if hit:
            return hit
    png = draw_scene(source.scenario or "tower", tick, boxes, source.name)
    with _frame_lock:
        _frame_cache[key] = png
        if len(_frame_cache) > 32:
            for old in list(_frame_cache)[: len(_frame_cache) - 32]:
                _frame_cache.pop(old, None)
    return png


def _should_alert(db: Session, rule: DetectionRule, source: VisionSource) -> bool:
    latest = db.scalar(
        select(DetectionAlert)
        .where(DetectionAlert.source_id == source.id, DetectionAlert.rule_id == rule.id)
        .order_by(DetectionAlert.id.desc())
    )
    if latest is None:
        return True
    wait = int(rule.cooldown_sec or 20)
    elapsed = (utcnow() - latest.created_at).total_seconds()
    return elapsed >= wait


def _push_event(db: Session, alert: DetectionAlert, source: VisionSource, rule: DetectionRule) -> None:
    if not rule.auto_push or not rule.agent_id or alert.event_id:
        return
    agent = db.get(Agent, rule.agent_id)
    if agent is None or not agent.allow_events:
        return
    ev = Event(
        agent_id=agent.id,
        title=alert.title,
        level=alert.level,
        source=f"视觉检测 · {source.code or source.name}",
        payload={
            "data": {
                "site": source.name,
                "code": source.code,
                "time": datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S"),
                "findings": alert.findings,
                "confidence": {item["id"]: item.get("confidence") for item in (alert.boxes or [])},
                "detector": alert.detector,
                "rule": rule.name,
            }
        },
        status="processing",
        created_by=source.owner_id,
    )
    db.add(ev)
    db.flush()
    alert.event_id = ev.id
    alert.status = "pushed"


class VisionRuntime:
    def __init__(self):
        self._lock = threading.Lock()
        self._ticks: dict[int, int] = {}
        self._boxes: dict[int, list[dict]] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start_loop(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="youju-vision")
        self._thread.start()

    def stop_loop(self) -> None:
        self._stop.set()

    def tick_of(self, source_id: int) -> int:
        return self._ticks.get(source_id, 0)

    def boxes_of(self, source_id: int) -> list[dict]:
        return list(self._boxes.get(source_id, []))

    def _loop(self) -> None:
        while not self._stop.wait(1.0):
            try:
                self.tick_live()
            except Exception:  # noqa: BLE001
                log.exception("vision tick failed")

    def tick_live(self) -> list[int]:
        db = SessionLocal()
        event_ids: list[int] = []
        try:
            with self._lock:
                live = db.scalars(select(VisionSource).where(VisionSource.status == "live")).all()
                if not live:
                    return []
                rules = db.scalars(select(DetectionRule).where(DetectionRule.enabled.is_(True))).all()
                for source in live:
                    tick = self._ticks.get(source.id, 0) + 1
                    self._ticks[source.id] = tick
                    hits = detect_frame(source, rules, tick)
                    self._boxes[source.id] = hits
                    grouped: dict[int, list[dict]] = {}
                    for hit in hits:
                        grouped.setdefault(int(hit["rule_id"]), []).append(hit)
                    for rule_id, group in grouped.items():
                        rule = next((r for r in rules if r.id == rule_id), None)
                        if rule is None or not _should_alert(db, rule, source):
                            continue
                        names = [item["name"] for item in group]
                        alert = DetectionAlert(
                            source_id=source.id,
                            rule_id=rule.id,
                            title=f"{source.name} 检测到{'、'.join(names)}",
                            level=rule.level or group[0]["level"],
                            findings=names,
                            boxes=group,
                            confidence=max(item["confidence"] for item in group),
                            detector=rule.detector,
                            tick=tick,
                            status="open",
                        )
                        db.add(alert)
                        db.flush()
                        _push_event(db, alert, source, rule)
                        if alert.event_id:
                            event_ids.append(alert.event_id)
                db.commit()
        finally:
            db.close()
        for event_id in event_ids:
            _process_event(event_id)
        return event_ids

    def start_source(self, db: Session, source: VisionSource) -> VisionSource:
        source.status = "live"
        source.live_started_at = utcnow()
        source.last_error = None
        db.commit()
        self._ticks.setdefault(source.id, 0)
        return source

    def stop_source(self, db: Session, source: VisionSource) -> VisionSource:
        source.status = "idle"
        db.commit()
        self._boxes.pop(source.id, None)
        return source


runtime = VisionRuntime()
_event_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="youju-vision-ev")


def _run_event(event_id: int) -> None:
    try:
        import asyncio

        from youju.routers.events import process_event

        asyncio.run(process_event(event_id))
    except Exception:  # noqa: BLE001
        log.exception("vision event %s failed", event_id)


def _process_event(event_id: int) -> None:
    _event_pool.submit(_run_event, event_id)


def find_source_by_name(db: Session, name: str, owner_id: int | None = None) -> VisionSource | None:
    needle = (name or "").strip()
    if not needle:
        return None
    rows = db.scalars(select(VisionSource).order_by(VisionSource.id)).all()
    exact = [s for s in rows if s.name == needle or s.code == needle]
    if exact:
        return exact[0]
    fuzzy = sorted((s for s in rows if needle in s.name or needle in (s.code or "")), key=lambda s: len(s.name))
    return fuzzy[0] if fuzzy else None
