"""有据 YouJu 的 FastAPI 入口。浏览器打开 http://127.0.0.1:8000 ，接口文档在 /docs。"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from youju import __version__
from youju.config import WEB_DIR
from youju.db import init_db
from youju.routers import agents, apikeys, auth, chat, evals, events, kbs, mcp, openai_api, providers, stats, users, vision
from youju.startup import bootstrap

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    bootstrap()
    yield
    from youju.vision.engine import runtime

    runtime.stop_loop()


app = FastAPI(title="有据 YouJu", version=__version__, lifespan=lifespan, redoc_url=None)

for module in (auth, users, providers, kbs, agents, chat, events, evals, stats, apikeys, openai_api, mcp, vision):
    app.include_router(module.router)


class WebFiles(StaticFiles):
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-store"
        return response


app.mount("/static", WebFiles(directory=WEB_DIR), name="static")


@app.exception_handler(RequestValidationError)
async def validation_error(_request: Request, exc: RequestValidationError):
    errors = exc.errors()
    first = errors[0] if errors else {}
    where = ".".join(str(p) for p in first.get("loc", []) if p not in ("body", "query", "path"))
    return JSONResponse(status_code=422, content={"detail": f"参数不正确：{where} {first.get('msg', '')}".strip()})


def os_theme() -> str:
    """Windows 应用主题。浏览器自己的 prefers-color-scheme 经常和系统不一致。"""
    if os.name != "nt":
        return "light"
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return "light" if int(value) else "dark"
    except OSError:
        return "light"


def _theme_choice(request: Request) -> str:
    pref = request.cookies.get("yj-theme")
    return pref if pref in ("light", "dark", "system") else "system"


@app.get("/", include_in_schema=False)
def index(request: Request):
    system = os_theme()
    choice = _theme_choice(request)
    theme = choice if choice in ("light", "dark") else system
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    html = html.replace(
        'data-os-theme="dark" data-theme="dark"',
        f'data-os-theme="{system}" data-theme="{theme}"',
        1,
    )
    html = html.replace('<meta name="color-scheme" content="dark" />', f'<meta name="color-scheme" content="{theme}" />', 1)
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(WEB_DIR / "favicon.svg", media_type="image/svg+xml")


@app.get("/api/health")
def health():
    return {"ok": True, "version": __version__}
