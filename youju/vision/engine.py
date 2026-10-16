"""拉流会话和逐帧检测。小模型用场景适配器；大模型必须看真实画面。"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from youju.config import settings
from youju.db import SessionLocal, utcnow
from youju.models import Agent, DetectionAlert, DetectionRule, Event, VisionSource
from youju.vision.catalog import SCENES, algo_level, algo_name
from youju.vision.frames import IMAGE_EXT, capture_jpeg, source_media_path
from youju.vision.render import draw_scene
from youju.vision.vlm import analyze_image, vision_ready
from youju.vision.yolo import detect_yolo

log = logging.getLogger("youju.vision")
CYCLE = 24
VLM_REVIEW_MIN = 0.7
# 视频：大模型看图较慢且计费，按 tick 节流
VLM_TICK_EVERY = 5
# 静态图：进入 live 后立即分析，之后按秒复检
IMAGE_VLM_INTERVAL_SEC = 10.0
# 同图同 finding 冷却，避免复检刷屏；不同 finding 可更快再告
IMAGE_SAME_FINDING_COOLDOWN = 60
IMAGE_NEW_FINDING_COOLDOWN = 8


def chat_ready(db: Session | None = None) -> bool:
    own = db is None
    session = db or SessionLocal()
    try:
        ok, _ = vision_ready(session)
        return ok
    finally:
        if own:
            session.close()


def is_static_image(source: VisionSource) -> bool:
    extra = getattr(source, "extra", None) or {}
    if extra.get("preview") == "image":
        return True
    mime = (getattr(source, "mime", None) or "").lower()
    if mime.startswith("image/"):
        return True
    media = source_media_path(source)
    if media and media.suffix.lower() in IMAGE_EXT:
        return True
    return False


def scenario_for_algorithms(algorithms: list[str] | None) -> str:
    wanted = set(algorithms or [])
    if not wanted:
        return "gate"
    best, best_n = "gate", -1
    for sid, scene in SCENES.items():
        ids = {obj["id"] for obj in scene["objects"]}
        n = len(wanted & ids)
        if n > best_n:
            best, best_n = sid, n
    return best


def visible_objects(scenario: str, tick: int) -> list[dict]:
    scene = SCENES.get(scenario) or SCENES["tower"]
    phase = tick % CYCLE
    found = []
    for obj in scene["objects"]:
        start, end = obj["phase"]
        if start <= phase <= end:
            found.append({**obj, "name": algo_name(obj["id"]), "level": algo_level(obj["id"])})
    return found


def match_rule(rule: DetectionRule, source: VisionSource, objects: list[dict], *, demo: bool = False) -> list[dict]:
    if not rule.enabled:
        return []
    if rule.source_id and rule.source_id != source.id:
        return []
    wanted = set(rule.algorithms or [])
    if not wanted:
        return []
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
                "detector": rule.detector or "small",
                "rule_id": rule.id,
                "from_image": False,
                "demo": demo,
            }
        )
    return hits


_frame_lock = threading.Lock()
_frame_cache: dict[tuple[int, int], bytes] = {}
_vlm_cache: dict[int, tuple[int, list[dict], bytes | None, str]] = {}  # source_id -> tick, hits, jpeg, hint
_vlm_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="youju-vlm")
_vlm_inflight: set[int] = set()
_vlm_inflight_lock = threading.Lock()
_last_vlm_at: dict[int, float] = {}


def render_source(source: VisionSource, tick: int, boxes: list[dict] | None = None) -> bytes:
    key = (source.id, int(tick))
    with _frame_lock:
        hit = _frame_cache.get(key)
        if hit:
            return hit
    media = source_media_path(source)
    if media:
        jpeg = capture_jpeg(media, tick=tick)
        if jpeg:
            with _frame_lock:
                _frame_cache[key] = jpeg
                if len(_frame_cache) > 32:
                    for old in list(_frame_cache)[: len(_frame_cache) - 32]:
                        _frame_cache.pop(old, None)
            return jpeg
    png = draw_scene(source.scenario or "tower", tick, boxes, source.name)
    with _frame_lock:
        _frame_cache[key] = png
        if len(_frame_cache) > 32:
            for old in list(_frame_cache)[: len(_frame_cache) - 32]:
                _frame_cache.pop(old, None)
    return png


def _save_snapshot(jpeg: bytes | None, source_id: int, tick: int) -> str:
    if not jpeg:
        return ""
    folder = settings.upload_dir / "vision" / "alerts" / str(source_id)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"t{tick}_{utcnow().strftime('%H%M%S')}.jpg"
    path.write_bytes(jpeg)
    return str(path)


def _vlm_hits_for_rule(
    db: Session,
    source: VisionSource,
    rule: DetectionRule,
    tick: int,
    jpeg: bytes | None,
) -> tuple[list[dict], str]:
    if jpeg is None:
        return [], "没有可分析的真实画面，请上传图片或视频后再用大模型检测"
    hits, err = analyze_image(
        db,
        jpeg,
        list(rule.algorithms or []),
        rule.id,
        detector=rule.detector or "vlm",
        instruction=rule.instruction or "",
        confidence_min=float(rule.confidence or 0.55),
        model_ref=getattr(rule, "vlm_model", None) or None,
    )
    if err:
        return [], err
    for hit in hits:
        hit["level"] = rule.level or hit.get("level") or "medium"
        hit["rule_id"] = rule.id
        hit["detector"] = rule.detector or "vlm"
    return hits, ""


def _should_run_vlm_now(source: VisionSource, tick: int, need_vlm: bool) -> bool:
    if not need_vlm:
        return False
    if is_static_image(source):
        last = _last_vlm_at.get(source.id)
        if last is None:
            return True
        return (time.monotonic() - last) >= IMAGE_VLM_INTERVAL_SEC
    return tick % VLM_TICK_EVERY == 1 or tick <= 1


def _small_hits(rule: DetectionRule, source: VisionSource, tick: int, jpeg: bytes | None) -> tuple[list[dict], str]:
    kind = (getattr(rule, "small_model", None) or "demo").strip() or "demo"
    if kind == "yolo" or kind.startswith("yolo:"):
        hits, hint = detect_yolo(jpeg, list(rule.algorithms or []), float(rule.confidence or 0.55), rule.id, kind)
        for hit in hits:
            hit["level"] = rule.level or hit.get("level") or "medium"
            hit["detector"] = rule.detector or "small"
        return hits, hint
    objects = visible_objects(source.scenario or "tower", tick)
    return match_rule(rule, source, objects, demo=is_static_image(source)), ""


def detect_frame(
    source: VisionSource,
    rules: list[DetectionRule],
    tick: int,
    vlm_ok: bool = False,
    db: Session | None = None,
    allow_vlm_call: bool = True,
) -> tuple[list[dict], bool, str, bytes | None]:
    """返回 hits, skipped, hint, frame_jpeg（大模型用过的那帧）。

    allow_vlm_call=False 时只复用缓存的大模型结果（供异步路径在 tick 内不阻塞）。
    """
    media = source_media_path(source)
    jpeg: bytes | None = None
    if media:
        jpeg = capture_jpeg(media, tick=tick)

    hits: list[dict] = []
    skipped = False
    hint = ""
    used_jpeg: bytes | None = None

    need_vlm = any((r.detector or "small") in ("vlm", "small_then_vlm") for r in rules if r.enabled)
    run_vlm_now = allow_vlm_call and _should_run_vlm_now(source, tick, need_vlm)
    cached = _vlm_cache.get(source.id)

    for rule in rules:
        if not rule.enabled:
            continue
        if rule.source_id and rule.source_id != source.id:
            continue
        mode = rule.detector or "small"

        if mode == "small":
            small, small_hint = _small_hits(rule, source, tick, jpeg)
            hits.extend(small)
            if small_hint:
                skipped = True
                hint = small_hint
            continue

        if not vlm_ok:
            skipped = True
            hint = "未配置大模型，无法对真实画面做看图分析"
            continue

        if jpeg is None:
            skipped = True
            hint = "没有可分析的真实画面，请上传图片或视频"
            continue

        if not run_vlm_now and cached:
            _c_tick, c_hits, c_jpeg, c_hint = cached
            reused = [h for h in c_hits if h.get("rule_id") == rule.id]
            if reused:
                hits.extend(reused)
                used_jpeg = c_jpeg
                if c_hint:
                    hint = c_hint
                continue
            if not allow_vlm_call:
                skipped = True
                hint = c_hint or "大模型分析进行中…"
                continue

        if not run_vlm_now and not cached and not allow_vlm_call:
            skipped = True
            hint = "大模型分析进行中…"
            continue

        if not run_vlm_now:
            continue

        if db is None:
            skipped = True
            hint = "内部错误：缺少数据库会话，无法调用大模型"
            continue

        vlm_hits, vlm_hint = _vlm_hits_for_rule(db, source, rule, tick, jpeg)
        if vlm_hint:
            skipped = True
            hint = vlm_hint
            continue

        if mode == "small_then_vlm":
            small, _small_hint = _small_hits(rule, source, tick, jpeg)
            small_ids = {h["id"] for h in small if float(h["confidence"]) >= VLM_REVIEW_MIN}
            confirmed = []
            for hit in vlm_hits:
                if hit["id"] in small_ids or float(hit["confidence"]) >= float(rule.confidence or 0.55):
                    hit["detector"] = "small_then_vlm"
                    confirmed.append(hit)
            hits.extend(confirmed if small else vlm_hits)
        else:
            hits.extend(vlm_hits)
        used_jpeg = jpeg

    if need_vlm and run_vlm_now and jpeg is not None:
        _vlm_cache[source.id] = (tick, [h for h in hits if h.get("from_image")], jpeg, hint)
        _last_vlm_at[source.id] = time.monotonic()

    return hits, skipped, hint, used_jpeg or jpeg


def _should_alert(db: Session, rule: DetectionRule, source: VisionSource, findings: list[str] | None = None) -> bool:
    latest = db.scalar(
        select(DetectionAlert)
        .where(DetectionAlert.source_id == source.id, DetectionAlert.rule_id == rule.id)
        .order_by(DetectionAlert.id.desc())
    )
    if latest is None:
        return True
    wait = int(rule.cooldown_sec or 20)
    if is_static_image(source):
        same = findings is not None and set(latest.findings or []) == set(findings)
        wait = IMAGE_SAME_FINDING_COOLDOWN if same else min(wait, IMAGE_NEW_FINDING_COOLDOWN)
    elapsed = (utcnow() - latest.created_at).total_seconds()
    return elapsed >= wait


def _push_event(db: Session, alert: DetectionAlert, source: VisionSource, rule: DetectionRule) -> None:
    if not rule.auto_push or not rule.agent_id or alert.event_id:
        return
    agent = db.get(Agent, rule.agent_id)
    if agent is None or not agent.allow_events:
        return
    evidence = list(getattr(alert, "evidence", None) or [])
    code = (source.code or "").strip() or (source.filename or "").strip() or f"src-{source.id}"
    ev = Event(
        agent_id=agent.id,
        title=alert.title,
        level=alert.level,
        source=f"视觉检测 · {code if source.code else source.name}",
        payload={
            "data": {
                "site": source.name,
                "code": code,
                "time": datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S"),
                "findings": alert.findings,
                "confidence": {item["id"]: item.get("confidence") for item in (alert.boxes or [])},
                "detector": alert.detector,
                "rule": rule.name,
                "evidence": evidence,
                "snapshot_url": f"/api/vision/alerts/{alert.id}/snapshot" if alert.snapshot_path else "",
                "alert_id": alert.id,
            }
        },
        status="processing",
        created_by=source.owner_id,
    )
    db.add(ev)
    db.flush()
    alert.event_id = ev.id
    alert.status = "pushed"


def _emit_alerts(
    db: Session,
    source: VisionSource,
    rules: list[DetectionRule],
    hits: list[dict],
    tick: int,
    frame_jpeg: bytes | None,
) -> list[int]:
    event_ids: list[int] = []
    grouped: dict[int, list[dict]] = {}
    for hit in hits:
        grouped.setdefault(int(hit["rule_id"]), []).append(hit)
    for rule_id, group in grouped.items():
        rule = next((r for r in rules if r.id == rule_id), None)
        if rule is None:
            continue
        names = [item["name"] for item in group]
        if not _should_alert(db, rule, source, names):
            continue
        mode = rule.detector or "small"
        if mode in ("vlm", "small_then_vlm"):
            if not any(h.get("from_image") and h.get("reason") for h in group):
                continue
            if not frame_jpeg:
                continue
        # 静态图仅小模型演示命中：不落真实告警，避免相位动画刷事件
        if mode == "small" and is_static_image(source) and all(h.get("demo") for h in group):
            continue
        evidence = [
            {"id": item["id"], "name": item["name"], "reason": item.get("reason") or "", "confidence": item.get("confidence")}
            for item in group
            if item.get("reason") or item.get("from_image")
        ]
        snap = _save_snapshot(frame_jpeg, source.id, tick) if mode in ("vlm", "small_then_vlm") else ""
        if not snap and frame_jpeg and source.kind == "file":
            snap = _save_snapshot(frame_jpeg, source.id, tick)
        alert = DetectionAlert(
            source_id=source.id,
            rule_id=rule.id,
            title=f"{source.name} 检测到{'、'.join(names)}",
            level=rule.level or group[0]["level"],
            findings=names,
            boxes=group,
            confidence=max(item["confidence"] for item in group),
            detector=group[0].get("detector") or rule.detector,
            tick=tick,
            snapshot_path=snap,
            evidence=evidence,
            status="open",
        )
        db.add(alert)
        db.flush()
        _push_event(db, alert, source, rule)
        if alert.event_id:
            event_ids.append(alert.event_id)
    return event_ids


def _set_source_extra(db: Session, source_id: int, **fields) -> None:
    src = db.get(VisionSource, source_id)
    if src is None:
        return
    extra = dict(src.extra or {})
    extra.update(fields)
    src.extra = extra


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

    def _schedule_vlm(
        self,
        source_id: int,
        tick: int,
        jpeg: bytes,
        rule_ids: list[int],
    ) -> None:
        with _vlm_inflight_lock:
            if source_id in _vlm_inflight:
                return
            _vlm_inflight.add(source_id)
        _last_vlm_at[source_id] = time.monotonic()
        _vlm_pool.submit(self._vlm_job, source_id, tick, jpeg, list(rule_ids))

    def _vlm_job(self, source_id: int, tick: int, jpeg: bytes, rule_ids: list[int]) -> None:
        event_ids: list[int] = []
        try:
            db = SessionLocal()
            try:
                source = db.get(VisionSource, source_id)
                if source is None or source.status != "live":
                    return
                rules = [
                    r
                    for r in db.scalars(select(DetectionRule).where(DetectionRule.enabled.is_(True))).all()
                    if r.id in set(rule_ids) and (not r.source_id or r.source_id == source_id)
                ]
                if not rules:
                    return
                if not chat_ready(db):
                    with self._lock:
                        _set_source_extra(db, source_id, vlm_skipped=True, vlm_hint="未配置大模型，无法对真实画面做看图分析", vlm_analyzed=False)
                        db.commit()
                    return

                all_hits: list[dict] = []
                hint = ""
                skipped = False
                for rule in rules:
                    mode = rule.detector or "small"
                    if mode not in ("vlm", "small_then_vlm"):
                        continue
                    vlm_hits, vlm_hint = _vlm_hits_for_rule(db, source, rule, tick, jpeg)
                    if vlm_hint:
                        skipped = True
                        hint = vlm_hint
                        continue
                    if mode == "small_then_vlm":
                        small, _small_hint = _small_hits(rule, source, tick, jpeg)
                        small_ids = {h["id"] for h in small if float(h["confidence"]) >= VLM_REVIEW_MIN}
                        confirmed = []
                        for hit in vlm_hits:
                            if hit["id"] in small_ids or float(hit["confidence"]) >= float(rule.confidence or 0.55):
                                hit["detector"] = "small_then_vlm"
                                confirmed.append(hit)
                        all_hits.extend(confirmed if small else vlm_hits)
                    else:
                        all_hits.extend(vlm_hits)

                _vlm_cache[source_id] = (tick, [h for h in all_hits if h.get("from_image")], jpeg, hint)
                with self._lock:
                    # 合并：保留当前 tick 的小模型 demo 框 + 新的 VLM 框
                    prev = [h for h in self._boxes.get(source_id, []) if h.get("demo") or (h.get("detector") or "") == "small"]
                    merged = prev + all_hits
                    self._boxes[source_id] = merged
                    _set_source_extra(
                        db,
                        source_id,
                        vlm_skipped=skipped,
                        vlm_hint=hint,
                        vlm_analyzed=True,
                    )
                    event_ids = _emit_alerts(db, source, rules, all_hits, tick, jpeg)
                    db.commit()
            finally:
                db.close()
        except Exception:  # noqa: BLE001
            log.exception("async vlm job failed for source %s", source_id)
        finally:
            with _vlm_inflight_lock:
                _vlm_inflight.discard(source_id)
        for event_id in event_ids:
            _process_event(event_id)

    def tick_live(self) -> list[int]:
        db = SessionLocal()
        event_ids: list[int] = []
        try:
            with self._lock:
                live = db.scalars(select(VisionSource).where(VisionSource.status == "live")).all()
                if not live:
                    return []
                rules = list(db.scalars(select(DetectionRule).where(DetectionRule.enabled.is_(True))).all())
                vlm_ok = chat_ready(db)
                for source in live:
                    tick = self._ticks.get(source.id, 0) + 1
                    self._ticks[source.id] = tick

                    applicable = [r for r in rules if r.enabled and (not r.source_id or r.source_id == source.id)]
                    need_vlm = any((r.detector or "small") in ("vlm", "small_then_vlm") for r in applicable)
                    want_vlm_now = _should_run_vlm_now(source, tick, need_vlm and vlm_ok)

                    media = source_media_path(source)
                    jpeg = capture_jpeg(media, tick=tick) if media else None

                    # 大模型走异步，tick 内不阻塞；小模型/演示仍同步
                    hits, skipped, hint, frame_jpeg = detect_frame(
                        source,
                        rules,
                        tick,
                        vlm_ok=vlm_ok,
                        db=db,
                        allow_vlm_call=False,
                    )
                    self._boxes[source.id] = hits
                    extra = dict(source.extra or {})
                    if want_vlm_now and jpeg and vlm_ok:
                        vlm_rule_ids = [
                            r.id
                            for r in applicable
                            if (r.detector or "small") in ("vlm", "small_then_vlm")
                        ]
                        if vlm_rule_ids:
                            with _vlm_inflight_lock:
                                pending = source.id in _vlm_inflight
                            if not pending:
                                hint = hint or "大模型分析进行中…"
                                skipped = True
                            self._schedule_vlm(source.id, tick, jpeg, vlm_rule_ids)
                    elif need_vlm and not vlm_ok:
                        skipped = True
                        hint = hint or "未配置大模型，无法对真实画面做看图分析"
                    # 仅小模型且静态图：标已「演示分析」
                    if not need_vlm and is_static_image(source):
                        extra["vlm_analyzed"] = True
                        if not hits:
                            hint = hint or "演示相位中暂无命中（小模型不读真实像素）"

                    extra["vlm_skipped"] = skipped
                    if hint:
                        extra["vlm_hint"] = hint
                    source.extra = extra

                    # 同步路径只发非 VLM（或已缓存的 from_image）告警；VLM 新结果由异步任务发
                    sync_hits = [h for h in hits if not h.get("from_image") or h.get("demo")]
                    # 缓存里的 VLM 命中也允许在冷却到期后再次发（复检）
                    cached_vlm = [h for h in hits if h.get("from_image")]
                    if cached_vlm and not want_vlm_now:
                        event_ids.extend(_emit_alerts(db, source, rules, cached_vlm, tick, frame_jpeg or jpeg))
                    event_ids.extend(_emit_alerts(db, source, rules, sync_hits, tick, frame_jpeg or jpeg))
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
        extra = dict(source.extra or {})
        extra["vlm_analyzed"] = False
        extra["vlm_hint"] = ""
        extra["vlm_skipped"] = False
        source.extra = extra
        db.commit()
        self._ticks.setdefault(source.id, 0)
        _vlm_cache.pop(source.id, None)
        _last_vlm_at.pop(source.id, None)
        with _vlm_inflight_lock:
            _vlm_inflight.discard(source.id)
        return source

    def stop_source(self, db: Session, source: VisionSource) -> VisionSource:
        source.status = "idle"
        db.commit()
        # 保留最后一帧 boxes，避免「停止才看见结果」的错觉
        _vlm_cache.pop(source.id, None)
        with _vlm_inflight_lock:
            _vlm_inflight.discard(source.id)
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
