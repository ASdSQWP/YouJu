"""有据 YouJu 的 FastAPI 入口。浏览器打开 http://127.0.0.1:8000 ，接口文档在 /docs。"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from youju import __version__
from youju.config import WEB_DIR
from youju.db import init_db
from youju.routers import agents, apikeys, auth, chat, evals, events, kbs, mcp, openai_api, providers, stats, users
from youju.startup import bootstrap

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    bootstrap()
    yield


app = FastAPI(title="有据 YouJu", version=__version__, lifespan=lifespan, redoc_url=None)

for module in (auth, users, providers, kbs, agents, chat, events, evals, stats, apikeys, openai_api, mcp):
    app.include_router(module.router)


class WebFiles(StaticFiles):
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/static", WebFiles(directory=WEB_DIR), name="static")


@app.exception_handler(RequestValidationError)
async def validation_error(_request: Request, exc: RequestValidationError):
    errors = exc.errors()
    first = errors[0] if errors else {}
    where = ".".join(str(p) for p in first.get("loc", []) if p not in ("body", "query", "path"))
    return JSONResponse(status_code=422, content={"detail": f"参数不正确：{where} {first.get('msg', '')}".strip()})


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(WEB_DIR / "favicon.svg", media_type="image/svg+xml")


@app.get("/api/health")
def health():
    return {"ok": True, "version": __version__}
