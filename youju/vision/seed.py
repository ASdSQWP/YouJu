"""已有管理员账号时补齐演示点位和安全巡检规则。"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from youju.models import Agent, DetectionRule, User, VisionSource
from youju.vision.catalog import DEMO_SOURCES


def seed_vision(db: Session, owner: User | None = None) -> None:
    if db.scalar(select(func.count(VisionSource.id))):
        return
    owner = owner or db.scalar(select(User).where(User.role == "admin").order_by(User.id))
    if owner is None:
        return
    safety = db.scalar(select(Agent).where(Agent.template == "safety").order_by(Agent.id))
    created: list[VisionSource] = []
    for item in DEMO_SOURCES:
        src = VisionSource(
            owner_id=owner.id,
            name=item["name"],
            code=item["code"],
            kind=item["kind"],
            uri=item["uri"],
            scenario=item["scenario"],
            status="idle",
        )
        db.add(src)
        db.flush()
        created.append(src)
    specs = [
        (created[0], ["no_belt", "no_hook", "edge_unguarded"], "high", "杆塔3 高空防护"),
        (created[1], ["fire_smoke", "no_supervisor"], "high", "动火区烟火与监护"),
        (created[2], ["no_helmet"], "medium", "工地大门安全帽"),
        (created[3], ["limit_intrusion"], "high", "吊臂限界侵入"),
    ]
    for source, algorithms, level, name in specs:
        db.add(
            DetectionRule(
                owner_id=owner.id,
                name=name,
                instruction=f"给{source.name}检测{'、'.join(algorithms)}，告警后推给安全巡检助手。",
                source_id=source.id,
                algorithms=algorithms,
                confidence=0.55,
                cooldown_sec=20,
                level=level,
                detector="small",
                agent_id=safety.id if safety else None,
                enabled=True,
                auto_push=True,
                auto_pull=False,
            )
        )
    db.commit()
