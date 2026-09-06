from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import AsyncSession

from atlas import __version__
from atlas.artifacts.models import ArtifactKind
from atlas.artifacts.service import ArtifactService
from atlas.artifacts.store import ArtifactStore
from atlas.config import get_settings
from atlas.db import database_health
from atlas.registry.service import build_phase0_registry
from atlas.runtime.bootstrap import build_seat_bootstrap
from atlas.runtime.startup import initialize_phase0

from .deps import get_session

settings = get_settings()
registry = build_phase0_registry()
artifact_store = ArtifactStore(settings.artifact_dir)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.startup_database_error = await initialize_phase0(settings, registry)
    yield

app = FastAPI(title="Atlas V5", version=__version__, lifespan=lifespan)


@app.get("/api/health")
async def health() -> JSONResponse:
    db_ok, db_error = await database_health()
    payload = {
        "status": "ok" if db_ok else "degraded",
        "version": __version__,
        "environment": settings.environment,
        "database": {"ok": db_ok, "error": db_error},
        "registry_entries": len(registry.all_entries()),
    }
    return JSONResponse(payload, status_code=200 if db_ok else 503)


@app.get("/api/bootstrap")
async def bootstrap():
    return build_seat_bootstrap()


@app.get("/api/registry")
async def registry_projection():
    return {"capabilities": registry.enabled_projection()}


@app.post("/api/artifacts")
async def upload_artifact(
    file: UploadFile,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    data = await file.read()
    media_type = file.content_type or "application/octet-stream"
    kind = ArtifactKind.IMAGE if media_type.startswith("image/") else ArtifactKind.FILE
    service = ArtifactService(session, artifact_store)
    return await service.persist(
        data,
        media_type=media_type,
        source="owner_upload",
        kind=kind,
        filename=file.filename,
    )


if settings.frontend_dist.is_dir():

    @app.get("/control", include_in_schema=False)
    @app.get("/control/", include_in_schema=False)
    async def control_page() -> FileResponse:
        return FileResponse(settings.frontend_dist / "index.html")

    app.mount("/", StaticFiles(directory=settings.frontend_dist, html=True), name="frontend")
