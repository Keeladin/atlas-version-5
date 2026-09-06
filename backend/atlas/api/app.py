import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from openai import APIError, OpenAIError
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from atlas import __version__
from atlas.actions.authority import AuthorityStore
from atlas.artifacts.models import ArtifactKind
from atlas.artifacts.service import ArtifactService
from atlas.artifacts.store import ArtifactStore
from atlas.capabilities.factory import build_capability_runtime
from atlas.config import get_settings
from atlas.db import database_health, get_session_factory
from atlas.integrations import GoogleWorkspaceService
from atlas.providers import OpenAIProvider
from atlas.registry.service import build_phase0_registry
from atlas.runtime.bootstrap import build_seat_bootstrap
from atlas.runtime.conversation import (
    build_model_instructions,
    turns_to_provider_messages,
)
from atlas.runtime.startup import initialize_phase0
from atlas.storage import LocalStorageService
from atlas.transcript.models import Actor, TextBlock
from atlas.transcript.repository import TranscriptRepository

from .deps import get_session

settings = get_settings()
registry = build_phase0_registry(settings)
artifact_store = ArtifactStore(settings.artifact_dir)
capability_runtime = build_capability_runtime(settings, registry)


class ChatRequest(BaseModel):
    text: str = ""
    attachments: list[str] = []


class ActionDecision(BaseModel):
    approve: bool


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.startup_database_error = await initialize_phase0(settings, registry)
    yield


app = FastAPI(title="Atlas V5", version=__version__, lifespan=lifespan)


def provider_projection() -> dict[str, str | bool | None]:
    configured = settings.openai_api_key is not None
    return {
        "provider": "OpenAI",
        "model": settings.openai_model,
        "configured": configured,
    }


@app.get("/api/health")
async def health() -> JSONResponse:
    db_ok, db_error = await database_health()
    payload = {
        "status": "ok" if db_ok else "degraded",
        "version": __version__,
        "environment": settings.environment,
        "database": {"ok": db_ok, "error": db_error},
        "provider": provider_projection(),
        "google_workspace": {"configured": settings.gws_configured},
        "registry_entries": len(registry.all_entries()),
    }
    return JSONResponse(payload, status_code=200 if db_ok else 503)


@app.get("/api/bootstrap")
async def bootstrap():
    return build_seat_bootstrap()


@app.get("/api/registry")
async def registry_projection():
    return {"capabilities": registry.enabled_projection()}


def _credential_projection(label: str, path) -> dict[str, object]:
    configured = path is not None and path.is_file()
    protected = False
    if configured:
        protected = (path.stat().st_mode & 0o077) == 0 or (path.stat().st_mode & 0o007) == 0
    return {
        "label": label,
        "configured": configured,
        "storage": "protected file" if path is not None else "not configured",
        "path": str(path) if path is not None else None,
        "protected": protected,
        "authenticated": None,
        "scope": None,
    }


def _google_oauth_projection() -> dict[str, object]:
    encrypted = settings.gws_config_dir / "credentials.enc"
    key = settings.gws_config_dir / ".encryption_key"
    configured = settings.gws_configured
    protected = configured and encrypted.is_file() and key.is_file()
    if protected:
        protected = all((path.stat().st_mode & 0o007) == 0 for path in (encrypted, key))
    authenticated = False
    scope = None
    if configured:
        service = GoogleWorkspaceService(
            settings.gws_command,
            settings.gws_credentials_file,
            settings.gws_config_dir,
            settings.gws_workspace_dir,
        )
        try:
            status = service.auth_status()
            authenticated = status.get("token_valid") is True
            scopes = status.get("scopes")
            if isinstance(scopes, list):
                labels = []
                if "https://www.googleapis.com/auth/drive" in scopes:
                    labels.append("Drive")
                elif "https://www.googleapis.com/auth/drive.readonly" in scopes:
                    labels.append("Drive read-only")
                if any("gmail" in str(item) or item == "https://mail.google.com/" for item in scopes):
                    labels.append("Gmail")
                scope = " + ".join(labels) or None
        except (RuntimeError, TypeError):
            authenticated = False
    return {
        "label": "Google Workspace OAuth",
        "configured": configured,
        "storage": "encrypted OAuth bundle",
        "path": str(settings.gws_config_dir) if configured else None,
        "protected": protected,
        "authenticated": authenticated,
        "scope": scope,
    }


@app.get("/api/control/configuration")
async def control_configuration():
    google = next((entry for entry in registry.all_entries() if entry.id == "google.workspace"), None)
    github = next((entry for entry in registry.all_entries() if entry.id == "github.mcp"), None)
    return {
        "credentials": [
            _credential_projection("OpenAI API key", settings.openai_api_key_file),
            _google_oauth_projection(),
            _credential_projection("GitHub MCP token", settings.github_token_file),
        ],
        "mcps": [
            {
                "id": "google.workspace",
                "label": "Google Workspace",
                "configured": settings.gws_configured,
                "enabled": bool(google and google.enabled),
                "availability": google.availability if google else "unavailable",
                "operations": google.executable_operations if google else [],
                "transport": "local Google Workspace bridge",
            },
            {
                "id": "github.mcp",
                "label": "GitHub",
                "configured": settings.github_configured,
                "enabled": bool(github and github.enabled),
                "availability": github.availability if github else "unavailable",
                "operations": github.executable_operations if github else [],
                "transport": "official GitHub MCP · stdio · read only",
            },
        ],
    }


@app.get("/api/actions/pending")
async def pending_actions(session: Annotated[AsyncSession, Depends(get_session)]):
    return {"items": await AuthorityStore(session).pending()}


@app.get("/api/actions/recent")
async def recent_actions(session: Annotated[AsyncSession, Depends(get_session)], limit: int = 8):
    return {"items": await AuthorityStore(session).recent_activity(limit)}


@app.post("/api/actions/{action_id}/decision")
async def decide_action(
    action_id: UUID,
    decision: ActionDecision,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    store = AuthorityStore(session)
    action = await store.action_for_decision(action_id)
    if action is None:
        raise HTTPException(status_code=404, detail="Action proposal not found")
    if action.status != "prepared":
        raise HTTPException(status_code=409, detail="Action proposal is no longer pending")
    proposal = (action.evidence or {}).get("proposal", {})
    arguments = proposal.get("arguments", {}) if isinstance(proposal, dict) else {}
    if not decision.approve:
        await store.resolve(action, approved=False, evidence={**(action.evidence or {}), "decision": "cancelled"})
        await session.commit()
        return {"status": "cancelled", "action_id": str(action.id)}
    result = await capability_runtime.call(action.operation, arguments if isinstance(arguments, dict) else {}, approval_granted=True)
    succeeded = result.status == "succeeded"
    await store.resolve(
        action,
        approved=True,
        succeeded=succeeded,
        evidence={**(action.evidence or {}), "decision": "approved", "result": result.model_dump(mode="json")},
    )
    await session.commit()
    return {"status": result.status, "action_id": str(action.id), "result": result}


def _context_pressure_state(input_tokens: int, limit_tokens: int) -> str:
    ratio = input_tokens / max(1, limit_tokens)
    if ratio >= 0.90:
        return "red"
    if ratio >= 0.70:
        return "amber"
    return "green"


@app.get("/api/conversation/context")
async def conversation_context(session: Annotated[AsyncSession, Depends(get_session)]):
    api_key = settings.openai_api_key
    if api_key is None:
        raise HTTPException(status_code=503, detail="OpenAI provider is not configured")
    repository = TranscriptRepository(session)
    transcript = await repository.get_or_create_active()
    turns = await repository.list_turns(transcript.id)
    provider = OpenAIProvider(api_key=api_key, model=settings.openai_model)
    input_tokens = await provider.count_input_tokens(
        instructions=build_model_instructions(capability_runtime.compact_index()),
        messages=turns_to_provider_messages(turns),
    )
    limit_tokens = settings.openai_context_window
    return {
        "input_tokens": input_tokens,
        "limit_tokens": limit_tokens,
        "pressure": input_tokens / max(1, limit_tokens),
        "state": _context_pressure_state(input_tokens, limit_tokens),
    }


@app.get("/api/conversation")
async def conversation(session: Annotated[AsyncSession, Depends(get_session)]):
    repository = TranscriptRepository(session)
    transcript = await repository.get_or_create_active()
    turns = await repository.list_turns(transcript.id)
    await session.commit()
    return {"transcript": transcript, "turns": turns}


@app.post("/api/conversation/stream")
async def stream_conversation(request: ChatRequest):
    text = request.text.strip()
    attachment_paths = [path.strip() for path in request.attachments if path.strip()]
    if not text and not attachment_paths:
        raise HTTPException(status_code=422, detail="Message text or an attachment is required")

    api_key = settings.openai_api_key
    if api_key is None:
        raise HTTPException(status_code=503, detail="OpenAI provider is not configured")

    factory = get_session_factory()
    async with factory() as session:
        repository = TranscriptRepository(session)
        transcript = await repository.get_or_create_active()
        await repository.append_turn(transcript.id, Actor.OWNER, [TextBlock(text=text)])
        run_intent = text or f"Attached {len(attachment_paths)} local workspace file(s)"
        run_id = await AuthorityStore(session).create_run(transcript_id=transcript.id, intent=run_intent)
        await session.commit()
        turns = await repository.list_turns(transcript.id)

    provider = OpenAIProvider(api_key=api_key, model=settings.openai_model)
    messages = turns_to_provider_messages(turns)
    if attachment_paths:
        attachment_note = (
            "Atlas runtime attached local workspace resource path(s) to the owner's current turn: "
            + ", ".join(attachment_paths)
            + ". Use storage.local.acquire to inspect them when relevant. "
            "This is runtime metadata, not owner-authored text."
        )
        messages.append({"role": "user", "content": attachment_note})

    async def tool_handler(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "atlas_capability_search":
            query = str(arguments.get("query") or "")
            limit = int(arguments.get("limit") or 8)
            return {"operations": [item.model_dump(mode="json") for item in capability_runtime.search(query, limit)]}
        if name == "atlas_capability_call":
            operation_id = str(arguments.get("operation_id") or "")
            operation_arguments = arguments.get("arguments")
            if not isinstance(operation_arguments, dict):
                operation_arguments = {}

            async def proposal_sink(descriptor, proposed_arguments):
                async with factory() as proposal_session:
                    store = AuthorityStore(proposal_session)
                    proposal_id = await store.prepare_proposal(
                        run_id=run_id,
                        operation=descriptor.id,
                        arguments=proposed_arguments,
                        title=f"Atlas proposes: {descriptor.description}",
                    )
                    await proposal_session.commit()
                    return proposal_id

            result = await capability_runtime.call(
                operation_id,
                operation_arguments,
                proposal_sink=proposal_sink,
            )
            if result.status != "approval_required":
                descriptor = next((item for item in capability_runtime.operations() if item.id == operation_id), None)
                async with factory() as activity_session:
                    await AuthorityStore(activity_session).record_execution(
                        run_id=run_id,
                        operation=operation_id,
                        arguments=operation_arguments,
                        status=result.status,
                        summary=descriptor.description if descriptor is not None else operation_id,
                    )
                    await activity_session.commit()
            return result.model_dump(mode="json")
        return {"status": "unavailable", "message": "Unknown Atlas capability control tool."}

    async def generate() -> AsyncIterator[str]:
        chunks: list[str] = []
        try:
            async for delta in provider.stream_text(
                instructions=build_model_instructions(capability_runtime.compact_index()),
                messages=messages,
                tool_handler=tool_handler,
            ):
                chunks.append(delta)
                yield json.dumps({"type": "delta", "text": delta}) + "\n"
        except APIError as exc:
            yield json.dumps({
                "type": "error",
                "message": f"OpenAI {type(exc).__name__}: {exc}",
            }) + "\n"
            return
        except OpenAIError as exc:
            yield json.dumps({
                "type": "error",
                "message": f"OpenAI {type(exc).__name__}: {exc}",
            }) + "\n"
            return

        answer = "".join(chunks).strip()
        if answer:
            async with factory() as session:
                repository = TranscriptRepository(session)
                await repository.append_turn(transcript.id, Actor.ATLAS, [TextBlock(text=answer)])
                await AuthorityStore(session).finish_run(run_id)
                await session.commit()
        yield json.dumps({"type": "done", "transcript_id": str(transcript.id)}) + "\n"

    return StreamingResponse(generate(), media_type="application/x-ndjson")


@app.get("/api/storage/local")
async def local_storage(path: str = ""):
    service = LocalStorageService(settings.workspace_root, settings.workspace_display_root)
    try:
        return service.list_directory(path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail="Local workspace is not available") from exc
    except NotADirectoryError as exc:
        raise HTTPException(status_code=400, detail="Requested path is not a directory") from exc
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

@app.get("/api/storage/drive")
async def drive_storage(folder_id: str = "root"):
    if not settings.gws_configured:
        raise HTTPException(status_code=503, detail="Google Workspace is not configured")
    service = GoogleWorkspaceService(
        settings.gws_command,
        settings.gws_credentials_file,
        settings.gws_config_dir,
        settings.gws_workspace_dir,
    )
    try:
        return service.list_drive_folder(folder_id)
    except (RuntimeError, TypeError) as exc:
        raise HTTPException(status_code=502, detail=f"Google Drive read failed: {exc}") from exc


@app.post("/api/storage/local/upload")
async def upload_local_storage(file: UploadFile, path: str = ""):
    data = await file.read()
    if len(data) > 100 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="File exceeds the 100 MB workspace upload limit")
    service = LocalStorageService(settings.workspace_root, settings.workspace_display_root)
    try:
        return service.store_file(path, file.filename or "upload", data)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail="Local workspace is not available") from exc
    except NotADirectoryError as exc:
        raise HTTPException(status_code=400, detail="Requested path is not a directory") from exc
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

@app.post("/api/artifacts")
async def upload_artifact(
    file: UploadFile,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    data = await file.read()
    media_type = file.content_type or "application/octet-stream"
    kind = ArtifactKind.IMAGE if media_type.startswith("image/") else ArtifactKind.FILE
    service = ArtifactService(session, artifact_store)
    artifact = await service.persist(
        data,
        media_type=media_type,
        source="owner_upload",
        kind=kind,
        filename=file.filename,
    )
    await session.commit()
    return artifact


if settings.frontend_dist.is_dir():

    @app.get("/control", include_in_schema=False)
    @app.get("/control/", include_in_schema=False)
    async def control_page() -> FileResponse:
        return FileResponse(settings.frontend_dist / "index.html")

    app.mount("/", StaticFiles(directory=settings.frontend_dist, html=True), name="frontend")
