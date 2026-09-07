import asyncio
import json
import os
import signal
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Annotated, Any
from uuid import UUID

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from openai import APIError, OpenAIError
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from atlas import __version__
from atlas.actions.authority import AuthorityStore, ProposalIntegrityError
from atlas.actions.models import ActionStatus
from atlas.actions.reconciliation import reconcile_once, reconciliation_loop
from atlas.artifacts.models import ArtifactKind
from atlas.artifacts.service import ArtifactService
from atlas.artifacts.store import ArtifactStore
from atlas.auth import SESSION_COOKIE_NAME, AuthService
from atlas.auth.api import router as auth_router
from atlas.capabilities import AuthorityMode, EffectKind
from atlas.capabilities.factory import build_capability_runtime
from atlas.config import get_settings
from atlas.db import database_health, get_session_factory
from atlas.integrations import GoogleWorkspaceService
from atlas.persistence.models import RunRow
from atlas.providers import OpenAIProvider
from atlas.registry.service import build_phase0_registry
from atlas.runtime.bootstrap import build_seat_bootstrap
from atlas.runtime.conversation import (
    build_model_instructions,
    context_turns,
    turns_to_provider_messages,
)
from atlas.runtime.startup import initialize_phase0
from atlas.schedules import ScheduleService
from atlas.schedules.runner import scheduler_loop
from atlas.storage import LocalStorageService, ProjectFolderService
from atlas.transcript.models import Actor, TextBlock, ToolObservationBlock
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
    if app.state.startup_database_error is None:
        await reconcile_once(settings)
    scheduler_task = asyncio.create_task(scheduler_loop(settings, capability_runtime)) if settings.scheduler_enabled else None
    reconciliation_task = asyncio.create_task(reconciliation_loop(settings))
    try:
        yield
    finally:
        for task in (scheduler_task, reconciliation_task):
            if task is not None:
                task.cancel()
        for task in (scheduler_task, reconciliation_task):
            if task is not None:
                with suppress(asyncio.CancelledError):
                    await task


app = FastAPI(title="Atlas V5", version=__version__, lifespan=lifespan)
app.include_router(auth_router)


def _auth_service(session: AsyncSession) -> AuthService:
    return AuthService(
        session,
        rp_id=settings.auth_rp_id,
        rp_name=settings.auth_rp_name,
        origin=settings.auth_origin,
        enrollment_code_file=settings.auth_enrollment_code_file,
        enrolled_marker_file=settings.auth_enrolled_marker_file,
        session_hours=settings.auth_session_hours,
    )


@app.middleware("http")
async def owner_auth_boundary(request: Request, call_next):
    path = request.url.path
    if settings.is_production and request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        origin = request.headers.get("origin")
        if path.startswith("/api/") and origin != settings.auth_origin:
            return JSONResponse({"detail": "Request origin is not allowed"}, status_code=403)

    protected = path.startswith("/api/") or path in {"/docs", "/redoc", "/openapi.json"}
    exempt = path.startswith("/api/auth/")
    if settings.auth_required and protected and not exempt:
        factory = get_session_factory()
        async with factory() as session:
            auth_session = await _auth_service(session).validate_session(request.cookies.get(SESSION_COOKIE_NAME))
            await session.commit()
        if auth_session is None:
            return JSONResponse({"detail": "Owner authentication required"}, status_code=401)

    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = "frame-ancestors 'none'; base-uri 'self'; object-src 'none'"
    if path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


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
                if any("calendar" in str(item) for item in scopes):
                    labels.append("Calendar")
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


def _restart_api_process() -> None:
    os.kill(os.getpid(), signal.SIGTERM)


@app.post("/api/control/restart", status_code=202)
async def restart_api(background_tasks: BackgroundTasks):
    background_tasks.add_task(_restart_api_process)
    return {"status": "restarting"}


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


@app.get("/api/schedules")
async def scheduled_tasks(session: Annotated[AsyncSession, Depends(get_session)], include_disabled: bool = True):
    return {"items": await ScheduleService(session, settings.owner_timezone).list(include_disabled=include_disabled)}


@app.get("/api/actions/pending")
async def pending_actions(session: Annotated[AsyncSession, Depends(get_session)]):
    return {"items": await AuthorityStore(session).pending()}


@app.get("/api/actions/recent")
async def recent_actions(session: Annotated[AsyncSession, Depends(get_session)], limit: int = 8):
    return {"items": await AuthorityStore(session).recent_activity(limit)}


@app.post("/api/actions/{action_id}/acknowledge")
async def acknowledge_action(action_id: UUID, session: Annotated[AsyncSession, Depends(get_session)]):
    try:
        await AuthorityStore(session).acknowledge_uncertain(action_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ProposalIntegrityError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await session.commit()
    return {"status": "acknowledged", "action_id": str(action_id)}


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
    if action.status != ActionStatus.PREPARED.value:
        raise HTTPException(status_code=409, detail="Action proposal is no longer pending")
    if not decision.approve:
        await store.cancel(action)
        await session.commit()
        return {"status": "cancelled", "action_id": str(action.id)}

    try:
        proposal = store.verify_proposal(action)
    except ProposalIntegrityError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    arguments = proposal["arguments"]
    descriptor = capability_runtime.descriptor(action.operation)
    if descriptor is None or descriptor.capability_id != proposal["capability_id"]:
        raise HTTPException(status_code=409, detail="Approved capability identity is no longer available")
    validation_error = capability_runtime.validate_arguments(action.operation, arguments)
    if validation_error:
        raise HTTPException(status_code=422, detail=validation_error)

    run = await session.get(RunRow, action.run_id)
    transcript_id = run.transcript_id if run is not None else None
    try:
        await store.begin_execution(action)
        await session.commit()  # durable executing state exists before external dispatch
    except ProposalIntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    try:
        result = await capability_runtime.call(action.operation, arguments, approval_granted=True)
    except Exception as exc:  # noqa: BLE001 - dispatch boundary is intentionally conservative
        result_payload = {"status": "uncertain", "operation_id": action.operation, "message": f"{type(exc).__name__}: {exc}"}
        async with get_session_factory()() as finish_session:
            await AuthorityStore(finish_session).complete(action_id, status=ActionStatus.UNCERTAIN, result=result_payload)
            await finish_session.commit()
        if transcript_id is not None:
            await _persist_tool_observation(transcript_id, action.operation, "uncertain", result_payload, action_id)
        return {"status": "uncertain", "action_id": str(action_id), "result": result_payload}

    result_payload = result.model_dump(mode="json")
    final_status = _action_status_for_result(result)
    external_id = _external_effect_id(result.output)
    async with get_session_factory()() as finish_session:
        await AuthorityStore(finish_session).complete(action_id, status=final_status, result=result_payload, external_id=external_id)
        await finish_session.commit()
    if transcript_id is not None:
        await _persist_tool_observation(transcript_id, action.operation, final_status.value, result_payload, action_id)
    return {"status": final_status.value, "action_id": str(action_id), "result": result}


def _action_status_for_result(result) -> ActionStatus:
    failure_phase = result.output.get("failure_phase") if isinstance(result.output, dict) else None
    if result.status == "succeeded":
        return ActionStatus.SUCCEEDED
    if result.status in {"unavailable", "forbidden"} or failure_phase == "before_dispatch":
        return ActionStatus.FAILED
    return ActionStatus.UNCERTAIN


def _external_effect_id(output: Any) -> str | None:
    if isinstance(output, dict):
        for key in ("id", "messageId", "message_id", "eventId", "event_id"):
            value = output.get(key)
            if isinstance(value, (str, int)) and str(value):
                return str(value)
        for value in output.values():
            found = _external_effect_id(value)
            if found:
                return found
    if isinstance(output, list):
        for value in output:
            found = _external_effect_id(value)
            if found:
                return found
    return None


async def _persist_tool_observation(transcript_id: UUID, operation: str, phase: str, detail: dict[str, Any], action_id: UUID | None = None) -> None:
    factory = get_session_factory()
    async with factory() as observation_session:
        repository = TranscriptRepository(observation_session)
        await repository.append_turn(
            transcript_id,
            Actor.TOOL,
            [ToolObservationBlock(action_id=action_id, operation=operation, phase=phase, summary=f"{operation} · {phase}", detail=detail)],
        )
        await observation_session.commit()


async def _prepare_provider_messages(provider: OpenAIProvider, transcript, turns):
    messages = turns_to_provider_messages(
        turns,
        context_summary=transcript.context_summary,
        summarized_through_turn_id=transcript.summarized_through_turn_id,
    )
    input_tokens = await provider.count_input_tokens(
        instructions=build_model_instructions(capability_runtime.compact_index()),
        messages=messages,
    )
    active_turns = context_turns(turns, transcript.summarized_through_turn_id)
    if input_tokens < int(settings.openai_context_window * 0.70) or len(active_turns) <= 40:
        return messages

    archive_turns = active_turns[:-40]
    summary_messages = turns_to_provider_messages(archive_turns)
    if transcript.context_summary:
        summary_messages.insert(0, {
            "role": "developer",
            "content": "Existing Atlas context capsule to update:\n" + transcript.context_summary,
        })
    summary = (await provider.complete_text(
        instructions=(
            "Create a compact durable Atlas context capsule. Preserve owner decisions, preferences, unresolved work, "
            "important factual context, and structured evidence of actions/results. Do not invent facts. "
            "Prefer concise operational continuity over conversational wording."
        ),
        messages=summary_messages,
    )).strip()
    if not summary:
        return messages

    summarized_through = archive_turns[-1].id
    factory = get_session_factory()
    async with factory() as rollover_session:
        await TranscriptRepository(rollover_session).update_context_summary(
            transcript.id, summary=summary, summarized_through_turn_id=summarized_through,
        )
        await rollover_session.commit()
    transcript.context_summary = summary
    transcript.summarized_through_turn_id = summarized_through
    return turns_to_provider_messages(
        turns,
        context_summary=summary,
        summarized_through_turn_id=summarized_through,
    )


def _context_pressure_state(input_tokens: int, limit_tokens: int) -> str:
    ratio = input_tokens / max(1, limit_tokens)
    if ratio >= 0.90:
        return "red"
    if ratio >= 0.70:
        return "amber"
    return "green"


def _recent_exchange_turns(turns, exchange_count: int):
    owner_indexes = [index for index, turn in enumerate(turns) if turn.actor == Actor.OWNER]
    if not owner_indexes or len(owner_indexes) <= exchange_count:
        return turns
    return turns[owner_indexes[-exchange_count]:]


def _turn_statistics(turns) -> dict[str, int]:
    owner_messages = 0
    atlas_messages = 0
    tool_observations = 0
    owner_characters = 0
    atlas_characters = 0
    largest_message_characters = 0
    for turn in turns:
        if turn.actor in (Actor.OWNER, Actor.ATLAS):
            text = "\n".join(
                block.text for block in turn.blocks if getattr(block, "type", None) == "text"
            ).strip()
            if turn.actor == Actor.OWNER:
                owner_messages += 1
                owner_characters += len(text)
            else:
                atlas_messages += 1
                atlas_characters += len(text)
            largest_message_characters = max(largest_message_characters, len(text))
        elif turn.actor == Actor.TOOL:
            tool_observations += sum(
                1 for block in turn.blocks if getattr(block, "type", None) == "tool_observation"
            )
    return {
        "owner_messages": owner_messages,
        "atlas_messages": atlas_messages,
        "tool_observations": tool_observations,
        "owner_characters": owner_characters,
        "atlas_characters": atlas_characters,
        "largest_message_characters": largest_message_characters,
        "transcript_turns": len(turns),
    }


@app.get("/api/conversation/context")
async def conversation_context(session: Annotated[AsyncSession, Depends(get_session)]):
    api_key = settings.openai_api_key
    if api_key is None:
        raise HTTPException(status_code=503, detail="OpenAI provider is not configured")
    repository = TranscriptRepository(session)
    transcript = await repository.get_or_create_active()
    turns = await repository.list_turns(transcript.id)
    provider = OpenAIProvider(api_key=api_key, model=settings.openai_model, capability_call_limit=settings.capability_call_limit, capability_completion_reserve=settings.capability_completion_reserve)
    input_tokens = await provider.count_input_tokens(
        instructions=build_model_instructions(capability_runtime.compact_index()),
        messages=turns_to_provider_messages(
            turns,
            context_summary=transcript.context_summary,
            summarized_through_turn_id=transcript.summarized_through_turn_id,
        ),
    )
    limit_tokens = settings.openai_context_window
    return {
        "input_tokens": input_tokens,
        "limit_tokens": limit_tokens,
        "pressure": input_tokens / max(1, limit_tokens),
        "state": _context_pressure_state(input_tokens, limit_tokens),
    }


@app.get("/api/conversation/context/stats")
async def conversation_context_stats(session: Annotated[AsyncSession, Depends(get_session)]):
    api_key = settings.openai_api_key
    if api_key is None:
        raise HTTPException(status_code=503, detail="OpenAI provider is not configured")
    repository = TranscriptRepository(session)
    transcript = await repository.get_or_create_active()
    turns = await repository.list_turns(transcript.id)
    provider = OpenAIProvider(
        api_key=api_key, model=settings.openai_model,
        capability_call_limit=settings.capability_call_limit,
        capability_completion_reserve=settings.capability_completion_reserve,
    )
    instructions = build_model_instructions(capability_runtime.compact_index())
    static_tokens = await provider.count_input_tokens(instructions=instructions, messages=[])
    current_messages = turns_to_provider_messages(
        turns, context_summary=transcript.context_summary,
        summarized_through_turn_id=transcript.summarized_through_turn_id,
    )
    current_tokens = await provider.count_input_tokens(instructions=instructions, messages=current_messages)
    canonical_tokens = await provider.count_input_tokens(
        instructions=instructions, messages=turns_to_provider_messages(turns)
    )
    windows = []
    for exchange_count in (5, 10, 15, 20):
        window_turns = _recent_exchange_turns(turns, exchange_count)
        window_messages = turns_to_provider_messages(
            window_turns, context_summary=transcript.context_summary
        )
        input_tokens = await provider.count_input_tokens(instructions=instructions, messages=window_messages)
        windows.append({
            "exchanges": exchange_count,
            "input_tokens": input_tokens,
            "dynamic_tokens": max(0, input_tokens - static_tokens),
            **_turn_statistics(window_turns),
        })
    limit_tokens = settings.openai_context_window
    return {
        "transcript_id": str(transcript.id),
        "static_tokens": static_tokens,
        "current_context_tokens": current_tokens,
        "canonical_transcript_tokens": canonical_tokens,
        "limit_tokens": limit_tokens,
        "pressure": current_tokens / max(1, limit_tokens),
        "state": _context_pressure_state(current_tokens, limit_tokens),
        "summary_present": bool(transcript.context_summary),
        "transcript": _turn_statistics(turns),
        "windows": windows,
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

    provider = OpenAIProvider(api_key=api_key, model=settings.openai_model, capability_call_limit=settings.capability_call_limit, capability_completion_reserve=settings.capability_completion_reserve)
    messages = await _prepare_provider_messages(provider, transcript, turns)
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
            search_result = {"operations": [item.model_dump(mode="json") for item in capability_runtime.search(query, limit)]}
            await _persist_tool_observation(transcript.id, "atlas_capability_search", "searched", {"query": query, "result": search_result})
            return search_result
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
                        capability_id=descriptor.capability_id,
                    )
                    await proposal_session.commit()
                    return proposal_id

            descriptor = capability_runtime.descriptor(operation_id)
            automatic_effect = (
                descriptor is not None
                and descriptor.authority == AuthorityMode.AUTO
                and descriptor.effect != EffectKind.READ
            )
            action_id = None
            if automatic_effect and descriptor is not None:
                async with factory() as activity_session:
                    action_id = await AuthorityStore(activity_session).begin_automatic_execution(
                        run_id=run_id, operation=operation_id, arguments=operation_arguments,
                        summary=descriptor.description, capability_id=descriptor.capability_id,
                    )
                    await activity_session.commit()

            result = await capability_runtime.call(
                operation_id,
                operation_arguments,
                proposal_sink=proposal_sink,
            )
            if result.status == "approval_required":
                proposal_uuid = UUID(result.proposal_id) if result.proposal_id else None
                await _persist_tool_observation(
                    transcript.id, operation_id, "prepared",
                    {"arguments": operation_arguments, "proposal_id": result.proposal_id, "status": result.status},
                    proposal_uuid,
                )
            elif automatic_effect and action_id is not None:
                final_status = _action_status_for_result(result)
                async with factory() as activity_session:
                    await AuthorityStore(activity_session).complete(
                        action_id, status=final_status, result=result.model_dump(mode="json"),
                        external_id=_external_effect_id(result.output), resolve_run=False,
                    )
                    await activity_session.commit()
                await _persist_tool_observation(
                    transcript.id, operation_id, final_status.value, result.model_dump(mode="json"), action_id,
                )
            else:
                async with factory() as activity_session:
                    action_id = await AuthorityStore(activity_session).record_execution(
                        run_id=run_id,
                        operation=operation_id,
                        arguments=operation_arguments,
                        status=result.status,
                        summary=descriptor.description if descriptor is not None else operation_id,
                        output=result.model_dump(mode="json"),
                    )
                    await activity_session.commit()
                await _persist_tool_observation(transcript.id, operation_id, result.status, result.model_dump(mode="json"), action_id)
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


@app.get("/api/storage/projects")
async def project_folders(path: str = ""):
    service = ProjectFolderService(settings.projects_root, settings.projects_display_root)
    try:
        return service.list_directory(path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail="Project folders are not available") from exc
    except NotADirectoryError as exc:
        raise HTTPException(status_code=400, detail="Requested path is not a directory") from exc
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def _repository_projection(output: Any) -> list[dict[str, Any]]:
    if isinstance(output, dict):
        candidates = output.get("items") or output.get("repositories") or output.get("nodes")
        if isinstance(candidates, list):
            return _repository_projection(candidates)
        for value in output.values():
            projected = _repository_projection(value)
            if projected:
                return projected
        return []
    if not isinstance(output, list):
        return []
    repositories: list[dict[str, Any]] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        name = item.get("name") or item.get("full_name")
        if not isinstance(name, str) or not name:
            continue
        full_name = str(item.get("full_name") or name)
        repositories.append({
            "name": name,
            "full_name": full_name,
            "url": item.get("html_url") or item.get("url"),
            "private": bool(item.get("private", False)),
            "archived": bool(item.get("archived", False)),
            "default_branch": item.get("default_branch"),
            "description": item.get("description"),
        })
    return repositories


@app.get("/api/repositories")
async def repositories():
    result = await capability_runtime.call(
        "github.search_repositories",
        {"query": f"user:{settings.github_owner}", "page": 1, "perPage": 100, "minimal_output": False},
    )
    if result.status != "succeeded":
        raise HTTPException(status_code=503, detail=result.message or "GitHub repositories are unavailable")
    items = _repository_projection(result.output)
    items.sort(key=lambda item: (item["archived"], item["name"].casefold()))
    return {"owner": settings.github_owner, "repositories": items}


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
