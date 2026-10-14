from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import ConfigError, load_config
from .jellyfin import JellyfinError, JellyfinReader
from .service import Service

try:
    CONFIG = load_config()
except ConfigError as exc:
    print(f"\nConfiguration problem: {exc}\n", file=sys.stderr)
    sys.exit(1)

reader = JellyfinReader(CONFIG.url, CONFIG.api_key, CONFIG.verify_tls, CONFIG.user_id)
service = Service(reader, CONFIG.cache_seconds)


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    await reader.close()


app = FastAPI(title="Jellyfin Collection Manager", lifespan=lifespan)


@app.exception_handler(JellyfinError)
async def jellyfin_error(_: Request, exc: JellyfinError):
    return JSONResponse({"error": str(exc)}, status_code=502)


@app.get("/api/snapshot")
async def snapshot(refresh: bool = False):
    return await service.snapshot(force=refresh)


@app.get("/api/selfcheck")
async def selfcheck():
    return {"checks": await service.selfcheck()}


app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")
