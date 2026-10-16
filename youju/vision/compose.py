"""把自然语言检测要求解析成规则。没有对话模型时走关键词，有模型时再尝试一次结构化输出。"""

from __future__ import annotations

import json
import re
from typing import Any

from youju.vision.catalog import ALGORITHMS, algo_level

_CONF = re.compile(r"(?:置信度|阈值|confidence)\s*[>=:：]?\s*(0?\.\d+|\d+(?:\.\d+)?)%?", re.I)
_COOL = re.compile(r"(?:冷却|间隔|cooldown)\s*[>=:：]?\s*(\d+)")


def _algorithms(text: str) -> list[str]:
    found: list[str] = []
    for item in ALGORITHMS:
        if item["id"] in found:
            continue
        if any(key in text for key in item["keywords"]):
            found.append(item["id"])
    return found


def _confidence(text: str) -> float:
    match = _CONF.search(text)
    if not match:
        return 0.55
    raw = float(match.group(1))
    if raw > 1:
        raw /= 100
    return round(min(0.99, max(0.1, raw)), 2)


def _cooldown(text: str) -> int:
    match = _COOL.search(text)
    if not match:
        return 20
    return int(min(300, max(3, int(match.group(1)))))


def _level(text: str, algorithms: list[str]) -> str:
    if any(word in text for word in ("低级", "当班", "不急")):
        return "low"
    if any(word in text for word in ("高级", "紧急", "立即", "P1", "critical")):
        return "high"
    ranks = [algo_level(a) for a in algorithms]
    if "high" in ranks:
        return "high"
    return "medium"


def _detector(text: str) -> str:
    if any(word in text for word in ("后处理", "复核", "小模型加大模型", "small_then_vlm")):
        return "small_then_vlm"
    if any(word in text for word in ("大模型", "视觉模型", "多模态", "vlm", "VLM", "仅大模型")):
        return "vlm"
    return "small"


def _match_source(text: str, sources: list[dict]) -> dict | None:
    ranked: list[tuple[int, dict]] = []
    for src in sources:
        score = 0
        name = str(src.get("name") or "")
        code = str(src.get("code") or "")
        if name and name in text:
            score += len(name) + 10
        if code and code in text:
            score += len(code) + 5
        head = name.split("-")[0].strip()
        if head and head in text:
            score += len(head)
        ranked.append((score, src))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked[0][1] if ranked and ranked[0][0] else None


def _match_agent(text: str, agents: list[dict]) -> dict | None:
    for agent in agents:
        if agent.get("name") and agent["name"] in text:
            return agent
    for agent in agents:
        if agent.get("template") == "safety" and any(word in text for word in ("安全", "巡检", "告警", "推给", "推送")):
            return agent
    return next((a for a in agents if a.get("template") == "safety"), None)


def parse_instruction(text: str, sources: list[dict] | None = None, agents: list[dict] | None = None) -> dict[str, Any]:
    instruction = (text or "").strip()
    if not instruction:
        raise ValueError("请先用自然语言描述要检测什么")
    algorithms = _algorithms(instruction)
    if not algorithms:
        names = "、".join(item["name"] for item in ALGORITHMS)
        raise ValueError(f"没有识别到检测项。可以写：{names}")
    source = _match_source(instruction, sources or [])
    agent = _match_agent(instruction, agents or [])
    auto_pull = any(word in instruction for word in ("拉流", "开始检测", "开始监视", "打开画面", "预览"))
    auto_push = "不推" not in instruction
    labels = "、".join(next(a["name"] for a in ALGORITHMS if a["id"] == i) for i in algorithms)
    target = source["name"] if source else "全部视频"
    name = f"{target} · {labels}"
    return {
        "name": name[:80],
        "instruction": instruction,
        "source_id": source["id"] if source else None,
        "source_name": source["name"] if source else "",
        "algorithms": algorithms,
        "confidence": _confidence(instruction),
        "cooldown_sec": _cooldown(instruction),
        "level": _level(instruction, algorithms),
        "detector": _detector(instruction),
        "agent_id": agent["id"] if agent else None,
        "agent_name": agent["name"] if agent else "",
        "enabled": True,
        "auto_push": auto_push,
        "auto_pull": auto_pull,
        "mode": "heuristic",
    }


def merge_model_json(draft: dict, raw: str) -> dict:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            return draft
        try:
            data = json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            return draft
    if not isinstance(data, dict):
        return draft
    allowed = {item["id"] for item in ALGORITHMS}
    algos = [a for a in (data.get("algorithms") or []) if a in allowed]
    if algos:
        draft["algorithms"] = algos
    for key in ("name", "level", "detector"):
        if data.get(key):
            draft[key] = data[key]
    if isinstance(data.get("confidence"), (int, float)):
        draft["confidence"] = round(min(0.99, max(0.1, float(data["confidence"]))), 2)
    if isinstance(data.get("cooldown_sec"), (int, float)):
        draft["cooldown_sec"] = int(min(300, max(3, data["cooldown_sec"])))
    if "auto_pull" in data:
        draft["auto_pull"] = bool(data["auto_pull"])
    if "auto_push" in data:
        draft["auto_push"] = bool(data["auto_push"])
    draft["mode"] = "model"
    return draft
