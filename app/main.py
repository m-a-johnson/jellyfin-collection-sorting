from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import ConfigError, load_config
from .jellyfin import CollectionWriter, JellyfinError, JellyfinReader, nid
from .service import ApplyError, Service

try:
    CONFIG = load_config()
except ConfigError as exc:
    print(f"\nConfiguration problem: {exc}\n", file=sys.stderr)
    sys.exit(1)

reader = JellyfinReader(CONFIG.url, CONFIG.api_key, CONFIG.verify_tls, CONFIG.user_id)
writer = CollectionWriter(CONFIG.url, CONFIG.api_key, CONFIG.verify_tls)
service = Service(reader, writer, CONFIG.cache_seconds, CONFIG.read_only)


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    await reader.close()
    await writer.close()


app = FastAPI(title="Jellyfin Collection Manager", lifespan=lifespan)


@app.exception_handler(JellyfinError)
async def jellyfin_error(_: Request, exc: JellyfinError):
    return JSONResponse({"error": str(exc)}, status_code=502)


@app.exception_handler(ApplyError)
async def apply_error(_: Request, exc: ApplyError):
    return JSONResponse({"error": str(exc)}, status_code=exc.status)


def require_app_header(request: Request) -> None:
    """The app has no login, so a page on another site must not be able to make
    your browser send changes to it. Browsers cannot add a custom header to a
    cross-site request without a CORS preflight, which this app never allows."""
    if request.headers.get("x-requested-with") != "jf-collections":
        raise HTTPException(403, "Missing X-Requested-With header.")


class ApplyBody(BaseModel):
    add: list[str] = Field(default_factory=list, max_length=500)
    remove: list[str] = Field(default_factory=list, max_length=500)
    dry_run: bool = False


class CreateBody(BaseModel):
    name: str = Field(max_length=200)
    dry_run: bool = False


class ItemCollectionsBody(BaseModel):
    add_to: list[str] = Field(default_factory=list, max_length=200)
    remove_from: list[str] = Field(default_factory=list, max_length=200)
    dry_run: bool = False


@app.get("/api/health")
async def health():
    """Liveness only: no call to Jellyfin, so the container is not marked unhealthy when Jellyfin restarts."""
    return {"status": "ok", "version": os.environ.get("APP_VERSION", "dev")}


@app.get("/api/snapshot")
async def snapshot(refresh: bool = False):
    return await service.snapshot(force=refresh)


@app.get("/api/selfcheck")
async def selfcheck():
    return {"checks": await service.selfcheck()}


@app.post("/api/collections", dependencies=[Depends(require_app_header)])
async def create_collection(body: CreateBody):
    return await service.create_collection(body.name, body.dry_run)


@app.post("/api/items/{item_id}/collections", dependencies=[Depends(require_app_header)])
async def set_item_collections(item_id: str, body: ItemCollectionsBody):
    return await service.set_item_collections(item_id, body.add_to, body.remove_from, body.dry_run)


@app.post("/api/collections/{collection_id}/apply", dependencies=[Depends(require_app_header)])
async def apply_changes(collection_id: str, body: ApplyBody):
    return await service.apply_changes(
        nid(collection_id), [nid(i) for i in body.add], [nid(i) for i in body.remove], body.dry_run)


app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")
