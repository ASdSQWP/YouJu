"""把一次回答写进数据库。网页对话和 OpenAI 兼容接口共用。"""

from __future__ import annotations

import logging

from youju.db import SessionLocal, utcnow
from youju.models import Conversation, Message

log = logging.getLogger("youju.answers")


def new_state() -> dict:
    return {"text": "", "reasoning": "", "sources": [], "trace": {}, "model": None, "mode": "", "error": None}


def track(state: dict, ev: dict) -> None:
    kind = ev.get("type")
    if kind == "meta":
        state["model"] = ev.get("model")
        state["mode"] = ev.get("mode", "")
    elif kind == "delta":
        state["text"] += ev["delta"]
    elif kind == "reasoning":
        state["reasoning"] += ev["delta"]
    elif kind == "interim_reset":
        state["text"] = ""
    elif kind == "replace":
        state["text"] = ev["content"]
    elif kind == "sources":
        state["sources"] = ev["sources"]
    elif kind == "step":
        state["trace"][ev["step"]["id"]] = ev["step"]
    elif kind == "error":
        state["error"] = ev["message"]


def save_answer(conv_id: int, final: dict | None = None, partial: dict | None = None) -> int | None:
    db = SessionLocal()
    try:
        if final is not None:
            model = final.get("model") or {}
            msg = Message(
                conv_id=conv_id,
                role="assistant",
                content=final.get("content", ""),
                reasoning=final.get("reasoning", ""),
                sources=final.get("sources", []),
                verification=final.get("check"),
                trace=final.get("trace", []),
                model_ref=model.get("ref"),
                model_label=model.get("label", ""),
                mode=final.get("mode", ""),
                usage=final.get("usage", {}),
                latency_ms=int(final.get("latency_ms") or 0),
                refused=bool(final.get("refused")),
                error=final.get("error"),
            )
        else:
            state = partial or new_state()
            model = state.get("model") or {}
            msg = Message(
                conv_id=conv_id,
                role="assistant",
                content=state["text"],
                reasoning=state["reasoning"],
                sources=state["sources"],
                trace=list(state["trace"].values()),
                model_ref=model.get("ref"),
                model_label=model.get("label", ""),
                mode=state.get("mode", ""),
                stopped=True,
                error=state.get("error"),
            )
        db.add(msg)
        conv = db.get(Conversation, conv_id)
        if conv is not None:
            conv.updated_at = utcnow()
        db.commit()
        return msg.id
    except Exception:  # noqa: BLE001
        log.exception("save answer failed")
        db.rollback()
        return None
    finally:
        db.close()
