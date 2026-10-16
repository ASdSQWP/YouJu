"""检测算法、演示点位和点位上会出现的目标。内容全部为作品演示虚构。"""

from __future__ import annotations

ALGORITHMS = [
    {"id": "no_helmet", "name": "未戴安全帽", "level": "medium", "keywords": ["安全帽", "未戴帽", "没戴帽", "头盔"]},
    {"id": "no_belt", "name": "安全带缺失", "level": "high", "keywords": ["安全带", "未系", "没扣", "没系"]},
    {"id": "no_hook", "name": "挂钩未连接", "level": "high", "keywords": ["挂钩", "未挂", "没挂", "未连接"]},
    {"id": "edge_unguarded", "name": "临边防护缺失", "level": "medium", "keywords": ["临边", "护栏", "防护缺失"]},
    {"id": "fire_smoke", "name": "烟火", "level": "high", "keywords": ["烟火", "火焰", "着火", "烟雾", "火情"]},
    {"id": "no_supervisor", "name": "无监护人", "level": "high", "keywords": ["监护人", "无人监护", "无监护"]},
    {"id": "limit_intrusion", "name": "限界侵入", "level": "high", "keywords": ["限界", "侵入", "越界", "吊臂"]},
]

DETECTORS = [
    {
        "id": "small",
        "name": "仅小模型",
        "hint": "目标检测算法。演示用点位适配器回放结果，接入后可换成 YOLO / 自研小模型。",
    },
    {
        "id": "vlm",
        "name": "仅大模型",
        "hint": "把抽帧画面送给多模态模型判断。未配置对话模型时回落到算法适配器，大模型为可选项。",
    },
    {
        "id": "small_then_vlm",
        "name": "小模型 + 大模型后处理",
        "hint": "先用小模型检出目标，再用大模型复核低置信度结果。未配置大模型时只跑小模型。",
    },
]

CATEGORIES = [
    {"id": "safety", "name": "高空安全"},
    {"id": "hotwork", "name": "动火作业"},
    {"id": "gate", "name": "出入口"},
    {"id": "limit", "name": "限界监测"},
    {"id": "other", "name": "其他"},
]

CATEGORY_BY_SCENARIO = {"tower": "safety", "hotwork": "hotwork", "gate": "gate", "limit": "limit"}

SCENES = {
    "tower": {
        "label": "高空作业",
        "palette": "night",
        "objects": [
            {"id": "no_belt", "box": [0.38, 0.28, 0.18, 0.46], "confidence": 0.91, "phase": (0, 18)},
            {"id": "no_hook", "box": [0.52, 0.22, 0.14, 0.18], "confidence": 0.84, "phase": (0, 16)},
            {"id": "edge_unguarded", "box": [0.18, 0.62, 0.46, 0.16], "confidence": 0.78, "phase": (1, 20)},
        ],
    },
    "hotwork": {
        "label": "动火作业",
        "palette": "ember",
        "objects": [
            {"id": "fire_smoke", "box": [0.28, 0.48, 0.32, 0.28], "confidence": 0.93, "phase": (0, 22)},
            {"id": "no_supervisor", "box": [0.62, 0.30, 0.22, 0.42], "confidence": 0.81, "phase": (0, 18)},
        ],
    },
    "gate": {
        "label": "出入口",
        "palette": "day",
        "objects": [
            {"id": "no_helmet", "box": [0.42, 0.26, 0.16, 0.48], "confidence": 0.88, "phase": (3, 14)},
            {"id": "no_helmet", "box": [0.62, 0.34, 0.14, 0.40], "confidence": 0.73, "phase": (10, 22)},
        ],
    },
    "limit": {
        "label": "限界监测",
        "palette": "yard",
        "objects": [
            {"id": "limit_intrusion", "box": [0.22, 0.18, 0.56, 0.22], "confidence": 0.9, "phase": (2, 16)},
        ],
    },
}

DEMO_SOURCES = [
    {
        "name": "杆塔3-登高作业",
        "code": "cam-03",
        "scenario": "tower",
        "kind": "demo",
        "uri": "demo://tower-3",
    },
    {
        "name": "动火区-电缆沟",
        "code": "cam-11",
        "scenario": "hotwork",
        "kind": "demo",
        "uri": "demo://hotwork-11",
    },
    {
        "name": "工地大门",
        "code": "cam-gate-01",
        "scenario": "gate",
        "kind": "demo",
        "uri": "demo://gate-01",
    },
    {
        "name": "限界监测-吊臂",
        "code": "cam-limit-07",
        "scenario": "limit",
        "kind": "demo",
        "uri": "demo://limit-07",
    },
]

ALGO_MAP = {item["id"]: item for item in ALGORITHMS}
SCENE_MAP = SCENES


def algo_name(algo_id: str) -> str:
    return ALGO_MAP.get(algo_id, {}).get("name", algo_id)


def algo_level(algo_id: str) -> str:
    return ALGO_MAP.get(algo_id, {}).get("level", "medium")
