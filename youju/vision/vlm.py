"""用视觉大模型看真实画面，返回带依据的检测结果。"""

from __future__ import annotations

import base64
import json
import logging
import re
from typing import Any

import httpx

from youju.providers import http
from youju.providers.base import ProviderError
from youju.providers.openai_compat import OpenAICompatProvider
from youju.providers.registry import LOCAL_CHAT, find_model, get_setting, list_models, chat_provider
from youju.vision.catalog import ALGORITHMS, algo_level, algo_name

log = logging.getLogger("youju.vision.vlm")

_JSON_BLOCK = re.compile(r"\{[\s\S]*\}")


def resolve_vision(db, model_ref: str | None = None):
    """优先用规则指定的模型，否则找带 vision 标记的对话模型，再退回任意非本地对话模型。"""
    preferred = (model_ref or "").strip() or get_setting(db, "default_chat_model")
    if preferred and preferred != LOCAL_CHAT:
        row, info = find_model(db, preferred)
        if row is not None and info is not None and info.type == "chat":
            return chat_provider(row), info, bool(getattr(info, "vision", False))
    vision_first = [i for i in list_models(db, "chat") if not i.local and getattr(i, "vision", False)]
    others = [i for i in list_models(db, "chat") if not i.local and i not in vision_first]
    for info in vision_first + others:
        row, found = find_model(db, info.ref)
        if row is not None and found is not None:
            return chat_provider(row), found, bool(getattr(found, "vision", False))
    return None, None, False


def vision_ready(db, model_ref: str | None = None) -> tuple[bool, str]:
    provider, info, is_vision = resolve_vision(db, model_ref)
    if provider is None or info is None:
        return False, "尚未配置可用于看图的大模型。请到「设置 → 模型服务」接入支持视觉的模型（如 Qwen-VL、GPT-4o）。"
    if not is_vision:
        return True, f"已接入 {info.label}，但未标记为视觉模型；将尝试送图分析，若接口不支持请换 VL 模型。"
    return True, f"已接入视觉模型 {info.label}，告警将基于真实画面分析。"


def _user_content(jpeg: bytes, prompt: str) -> list[dict]:
    b64 = base64.b64encode(jpeg).decode("ascii")
    return [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
    ]


def _prompt(algorithms: list[str], instruction: str = "") -> str:
    catalog = []
    for item in ALGORITHMS:
        if item["id"] in algorithms:
            catalog.append(f"- {item['id']}: {item['name']}")
    wanted = "\n".join(catalog) or "（未指定，请根据画面安全风险判断）"
    extra = f"\n规则说明：{instruction}" if instruction else ""
    return (
        "你是工地/厂区视觉安全检测助手。请只根据这张图片里能看见的内容判断，"
        "不要臆造画面中没有的目标。\n"
        f"需要关注的检测项：\n{wanted}{extra}\n\n"
        "只输出一个 JSON 对象，格式：\n"
        '{"findings":[{"id":"算法id","confidence":0.0到1.0,"box":[x,y,w,h],"reason":"引用画面中的可见依据"}]}\n'
        "box 为相对坐标，左上角为原点，取值 0~1（x,y 为左上角，w/h 为宽高）。"
        "若画面中没有对应风险，返回 {\"findings\":[]}。"
        "reason 必须写清依据，例如「画面左下工人未戴安全帽」。"
    )


def _parse_findings(text: str, algorithms: list[str], rule_id: int, detector: str) -> list[dict]:
    raw = (text or "").strip()
    if not raw:
        return []
    match = _JSON_BLOCK.search(raw)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    items = data.get("findings") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    allowed = set(algorithms) or {a["id"] for a in ALGORITHMS}
    out: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        algo_id = str(item.get("id") or "").strip()
        if algo_id not in allowed:
            continue
        try:
            conf = float(item.get("confidence") or 0)
        except (TypeError, ValueError):
            conf = 0.0
        conf = max(0.0, min(1.0, conf))
        box = item.get("box")
        if not (isinstance(box, list) and len(box) == 4):
            box = [0.1, 0.1, 0.3, 0.3]
        try:
            box = [max(0.0, min(1.0, float(x))) for x in box]
        except (TypeError, ValueError):
            box = [0.1, 0.1, 0.3, 0.3]
        reason = str(item.get("reason") or "").strip()[:240]
        if not reason:
            continue
        out.append(
            {
                "id": algo_id,
                "name": algo_name(algo_id),
                "box": box,
                "confidence": conf,
                "level": algo_level(algo_id),
                "detector": detector,
                "rule_id": rule_id,
                "reason": reason,
                "from_image": True,
            }
        )
    return out


def _complete_openai(provider: OpenAICompatProvider, model: str, messages: list[dict]) -> str:
    body = {
        "model": model,
        "messages": OpenAICompatProvider.wire_messages(messages),
        "stream": False,
        "temperature": 0.1,
    }
    url = f"{provider.base_url}/chat/completions"
    with http.sync_client(timeout=httpx.Timeout(connect=15.0, read=90.0, write=30.0, pool=15.0)) as client:
        resp = client.post(url, json=body, headers=provider.headers())
    if resp.status_code >= 400:
        raise error_from_status(resp.status_code, resp.content)
    payload = resp.json()
    choices = payload.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        return "".join(str(part.get("text") or "") for part in content if isinstance(part, dict))
    return str(content or "")


def error_from_status(status: int, body: bytes) -> ProviderError:
    from youju.providers.base import error_from_response

    return error_from_response(status, body)


def analyze_image(
    db,
    jpeg: bytes,
    algorithms: list[str],
    rule_id: int,
    detector: str = "vlm",
    instruction: str = "",
    confidence_min: float = 0.55,
    model_ref: str | None = None,
) -> tuple[list[dict], str | None]:
    """看图分析。返回 (hits, error_hint)。没有可见依据时 hits 为空。"""
    provider, info, _is_vision = resolve_vision(db, model_ref)
    if provider is None or info is None:
        return [], "未配置大模型，无法分析画面"
    if not isinstance(provider, OpenAICompatProvider):
        return [], f"当前协议 {getattr(provider, 'kind', type(provider))} 暂不支持送图，请改用 OpenAI 兼容的视觉模型"
    prompt = _prompt(algorithms, instruction)
    messages = [{"role": "user", "content": _user_content(jpeg, prompt)}]
    try:
        text = _complete_openai(provider, info.model, messages)
    except ProviderError as exc:
        log.warning("vlm analyze failed: %s", exc)
        return [], str(exc)
    except Exception as exc:  # noqa: BLE001
        log.exception("vlm analyze error")
        return [], f"大模型看图失败：{exc}"
    hits = _parse_findings(text, algorithms, rule_id, detector)
    hits = [h for h in hits if h["confidence"] >= float(confidence_min or 0)]
    return hits, None
