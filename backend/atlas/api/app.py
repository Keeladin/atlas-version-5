import asyncio
import json
import os
import signal
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from atlas import __version__
from atlas.actions.authority import (
    AuthorityStore,
    ForegroundBusy,
    ProposalIntegrityError,
)
from atlas.actions.models import ActionStatus
from atlas.actions.reconciliation import reconcile_once, reconciliation_loop
from atlas.artifacts.models import ArtifactKind
from atlas.artifacts.service import ArtifactService
from atlas.artifacts.store import ArtifactStore
from atlas.auth import SESSION_COOKIE_NAME, AuthService
from atlas.auth.api import router as auth_router
from atlas.capabilities.factory import build_capability_runtime
from atlas.capabilities.models import CapabilityCallResult
from atlas.config import get_settings
from atlas.db import database_health, get_session_factory
from atlas.integrations import GoogleWorkspaceService
from atlas.persistence.models import OwnerAttentionRow, RunRow
from atlas.providers import OpenAIProvider
from atlas.registry.repository import RegistryRepository
from atlas.registry.service import build_phase0_registry
from atlas.runtime.bootstrap import build_seat_bootstrap
from atlas.runtime.conversation import (
    build_model_instructions,
    context_turns,
    tool_observation_is_compactable,
    tool_observation_to_provider_message,
    tool_turn_exchange_ages,
    turns_to_provider_messages,
)
from atlas.runtime.conversation import (
    recent_exchange_turns as _recent_exchange_turns,
)
from atlas.runtime.execution import (
    RunExecutor,
    action_status_for_result,
    external_effect_id,
)
from atlas.runtime.observations import EvidenceStore
from atlas.runtime.recovery import interrupt_run, maintain_heartbeat, require_live_run
from atlas.runtime.startup import initialize_phase0
from atlas.runtime.task_state import (
    TaskStateDelta,
    active_task_provider_message,
    begin_owner_turn,
    merge_semantic_delta,
)
from atlas.schedules import ScheduleService
from atlas.schedules.runner import scheduler_loop
from atlas.storage import LocalStorageService, ProjectFolderService
from atlas.storage.changes import ProjectChanges
from atlas.transcript.models import Actor, ArtifactRefBlock, TextBlock
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
    reviewed_target_hash: str | None = None


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
    enabled = await capability_runtime.enabled_capabilities()
    return {"capabilities": [entry for entry in registry.all_entries() if entry.id in enabled]}


async def require_capability(capability_id: str):
    if capability_id not in await capability_runtime.enabled_capabilities():
        raise HTTPException(status_code=403, detail="This capability is disabled or unavailable. Enable it in Control before use.")


class CapabilitySetting(BaseModel):
    enabled: bool


@app.get("/api/control/capabilities")
async def owner_capabilities(session: Annotated[AsyncSession, Depends(get_session)]):
    return {"items": await RegistryRepository(session).owner_settings()}


@app.put("/api/control/capabilities/{capability_id}")
async def set_owner_capability(capability_id: str, setting: CapabilitySetting,
        session: Annotated[AsyncSession, Depends(get_session)]):
    try:
        await RegistryRepository(session).set_enabled(capability_id, setting.enabled)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await session.commit()
    return {"id": capability_id, "enabled": setting.enabled}


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
    enabled = await capability_runtime.enabled_capabilities()
    google = next((entry for entry in registry.all_entries() if entry.id == "google.workspace"), None)
    github = next((entry for entry in registry.all_entries() if entry.id == "github.mcp"), None)
    return {
        "credentials": [
            _credential_projection("OpenAI API key", settings.openai_api_key_file),
            await asyncio.to_thread(_google_oauth_projection),
            _credential_projection("GitHub MCP token", settings.github_token_file),
        ],
        "mcps": [
            {
                "id": "google.workspace",
                "label": "Google Workspace",
                "configured": settings.gws_configured,
                "enabled": bool(google and google.id in enabled),
                "availability": google.availability if google else "unavailable",
                "operations": google.executable_operations if google else [],
                "transport": "local Google Workspace bridge",
            },
            {
                "id": "github.mcp",
                "label": "GitHub",
                "configured": settings.github_configured,
                "enabled": bool(github and github.id in enabled),
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


@app.post("/api/attention/{attention_id}/dismiss")
async def dismiss_attention(attention_id: UUID, session: Annotated[AsyncSession, Depends(get_session)]):
    try:
        await AuthorityStore(session).dismiss_interruption(attention_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ProposalIntegrityError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await session.commit()
    return {"status": "dismissed", "attention_id": str(attention_id)}


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
        run = await session.get(RunRow, action.run_id)
        transcript_id = run.transcript_id if run is not None else None
        try:
            await store.cancel(action)
            if transcript_id is not None:
                await EvidenceStore(session, artifact_store).record(transcript_id, operation=action.operation,
                    phase="cancelled", detail={"status": "cancelled"}, run_id=action.run_id,
                    action_id=action.id, checkpoint=run.kind == "foreground", trust="internal")
            await session.commit()
        except ProposalIntegrityError as exc:
            await session.rollback()
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"status": "cancelled", "action_id": str(action.id)}

    if not decision.reviewed_target_hash:
        raise HTTPException(status_code=409, detail="Reload and review the exact proposal before approval")
    try:
        proposal = store.verify_proposal(action)
    except ProposalIntegrityError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    arguments = proposal["arguments"]
    descriptor = await capability_runtime.descriptor_current(action.operation)
    if descriptor is None or descriptor.capability_id != proposal["capability_id"]:
        raise HTTPException(status_code=409, detail="Approved capability identity is no longer available")
    validation_error = capability_runtime.validate_arguments(action.operation, arguments)
    if validation_error:
        raise HTTPException(status_code=422, detail=validation_error)

    run = await session.get(RunRow, action.run_id)
    transcript_id = run.transcript_id if run is not None else None
    try:
        await store.begin_execution(action, reviewed_target_hash=decision.reviewed_target_hash)
        if transcript_id is not None:
            await EvidenceStore(session, artifact_store).record(transcript_id, operation=action.operation,
                phase="executing", detail={"status": "executing"}, run_id=action.run_id,
                action_id=action.id, checkpoint=run.kind == "foreground", trust="internal")
        await session.commit()  # durable executing state exists before external dispatch
    except ProposalIntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    try:
        result = await capability_runtime.call(action.operation, arguments, approval_granted=True)
    except Exception as exc:  # noqa: BLE001 - dispatch boundary is intentionally conservative
        result = CapabilityCallResult(status="failed", operation_id=action.operation,
            output={"failure_phase": "ambiguous_dispatch"}, message=f"Dispatch interrupted ({type(exc).__name__})")

    if transcript_id is not None:
        executor = RunExecutor(get_session_factory(), capability_runtime, artifact_store,
            run_id=action.run_id, transcript_id=transcript_id, checkpoint=run.kind == "foreground")
        _, finalized = await executor.finish_action(action_id, result, arguments)
        final_status = finalized["action_status"]
    else:
        async with get_session_factory()() as finish_session:
            await AuthorityStore(finish_session).complete(action_id, status=_action_status_for_result(result),
                result={"status": result.status})
            await finish_session.commit()
        final_status = _action_status_for_result(result).value
    return {"status": final_status, "action_id": str(action_id)}


_action_status_for_result = action_status_for_result
_external_effect_id = external_effect_id


async def _persist_tool_observation(
    transcript_id: UUID,
    operation: str,
    phase: str,
    detail: dict[str, Any],
    action_id: UUID | None = None,
    *,
    arguments: dict[str, Any] | None = None,
    checkpoint_runtime: bool = True,
) -> UUID:
    async with get_session_factory()() as session:
        evidence_id, _ = await EvidenceStore(session, artifact_store).record(
            transcript_id, operation=operation, phase=phase, detail=detail,
            action_id=action_id, arguments=arguments, checkpoint=checkpoint_runtime)
        await session.commit()
        return evidence_id


async def _assemble_working_context(provider: OpenAIProvider, transcript, turns):
    instructions = build_model_instructions(await capability_runtime.compact_index_current())
    exchange_limit = max(1, settings.working_context_exchanges)
    token_budget = max(1, int(min(settings.working_context_tokens, settings.openai_context_window) * 0.75))
    raw_tool_exchanges = max(0, settings.working_context_raw_tool_exchanges)
    active_turns = context_turns(turns, transcript.summarized_through_turn_id)
    selected_turns = _recent_exchange_turns(active_turns, exchange_limit)
    summary = transcript.context_summary

    async def measure(candidate_turns, compact_ids: set[UUID], *, include_summary: bool = True):
        messages = turns_to_provider_messages(
            candidate_turns,
            context_summary=summary if include_summary else None,
            compact_tool_turn_ids=compact_ids,
        )
        task_message = active_task_provider_message(transcript.active_task_state)
        if task_message is not None:
            messages.insert(0, task_message)
        input_tokens = await provider.count_input_tokens(instructions=instructions, messages=messages)
        return messages, input_tokens

    def compactable_ids(candidate_turns) -> set[UUID]:
        result: set[UUID] = set()
        for turn in candidate_turns:
            if turn.actor != Actor.TOOL:
                continue
            observations = [
                block for block in turn.blocks
                if getattr(block, "type", None) == "tool_observation"
            ]
            if observations and all(tool_observation_is_compactable(block) for block in observations):
                result.add(turn.id)
        return result

    compact_ids: set[UUID] = set()
    messages, input_tokens = await measure(selected_turns, compact_ids)
    stage = "verbatim"

    if input_tokens > token_budget:
        ages = tool_turn_exchange_ages(selected_turns)
        compact_ids = {
            turn_id for turn_id in compactable_ids(selected_turns)
            if ages.get(turn_id, 1) > raw_tool_exchanges
        }
        messages, input_tokens = await measure(selected_turns, compact_ids)
        stage = "older_tools_compacted"

    if input_tokens > token_budget:
        compact_ids = compactable_ids(selected_turns)
        messages, input_tokens = await measure(selected_turns, compact_ids)
        stage = "successful_tools_compacted"

    owner_count = sum(1 for turn in selected_turns if turn.actor == Actor.OWNER)
    if input_tokens > token_budget and owner_count > 1:
        for exchange_count in range(owner_count - 1, 0, -1):
            trimmed = _recent_exchange_turns(selected_turns, exchange_count)
            trimmed_ids = compactable_ids(trimmed)
            candidate_messages, candidate_tokens = await measure(trimmed, trimmed_ids)
            selected_turns, compact_ids = trimmed, trimmed_ids
            messages, input_tokens = candidate_messages, candidate_tokens
            stage = "history_trimmed"
            if input_tokens <= token_budget:
                break

    summary_included = bool(summary)
    if input_tokens > token_budget and summary:
        messages, input_tokens = await measure(selected_turns, compact_ids, include_summary=False)
        summary_included = False
        stage = "capsule_omitted"

    selected_exchanges = sum(1 for turn in selected_turns if turn.actor == Actor.OWNER)
    return messages, {
        "input_tokens": input_tokens,
        "token_budget": settings.working_context_tokens,
        "initial_context_budget": token_budget,
        "exchange_limit": exchange_limit,
        "selected_exchanges": selected_exchanges,
        "compacted_tool_turns": len(compact_ids),
        "summary_included": summary_included,
        "budget_exceeded": input_tokens > token_budget,
        "stage": stage,
    }


async def _prepare_provider_messages(provider: OpenAIProvider, transcript, turns):
    messages, _ = await _assemble_working_context(provider, transcript, turns)
    return messages


def _context_pressure_state(input_tokens: int, limit_tokens: int) -> str:
    ratio = input_tokens / max(1, limit_tokens)
    if ratio >= 0.90:
        return "red"
    if ratio >= 0.70:
        return "amber"
    return "green"


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


def _tool_evidence_records(turns) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    owner_sequence = 0
    for turn in turns:
        if turn.actor == Actor.OWNER:
            owner_sequence += 1
            continue
        if turn.actor != Actor.TOOL:
            continue
        for block in turn.blocks:
            if getattr(block, "type", None) != "tool_observation":
                continue
            message = tool_observation_to_provider_message(block)
            records.append({
                "operation": getattr(block, "operation", None) or "runtime",
                "phase": getattr(block, "phase", None) or "observed",
                "exchange_sequence": owner_sequence,
                "payload_characters": len(message["content"]),
                "message": message,
            })
    for record in records:
        sequence = int(record["exchange_sequence"])
        record["exchange_age"] = max(1, owner_sequence - sequence + 1)
    return records


def _tool_band(exchange_age: int) -> str:
    if exchange_age <= 10:
        return "last_10"
    if exchange_age <= 15:
        return "exchanges_11_15"
    return "exchanges_16_20"


@app.get("/api/conversation/context")
async def conversation_context(session: Annotated[AsyncSession, Depends(get_session)]):
    api_key = settings.openai_api_key
    if api_key is None:
        raise HTTPException(status_code=503, detail="OpenAI provider is not configured")
    repository = TranscriptRepository(session)
    transcript = await repository.get_or_create_active()
    turns = await repository.list_recent_turns(transcript.id)
    provider = OpenAIProvider(api_key=api_key, model=settings.openai_model, capability_call_limit=settings.capability_call_limit, capability_completion_reserve=settings.capability_completion_reserve, capability_policy=capability_runtime.enabled_capabilities, input_token_budget=min(settings.working_context_tokens, settings.openai_context_window))
    _, policy = await _assemble_working_context(provider, transcript, turns)
    input_tokens = int(policy["input_tokens"])
    limit_tokens = int(policy["token_budget"])
    return {
        "input_tokens": input_tokens,
        "limit_tokens": limit_tokens,
        "provider_limit_tokens": settings.openai_context_window,
        "pressure": input_tokens / max(1, limit_tokens),
        "state": _context_pressure_state(input_tokens, limit_tokens),
        "policy": policy,
    }


@app.get("/api/conversation/context/stats")
async def conversation_context_stats(session: Annotated[AsyncSession, Depends(get_session)]):
    api_key = settings.openai_api_key
    if api_key is None:
        raise HTTPException(status_code=503, detail="OpenAI provider is not configured")
    repository = TranscriptRepository(session)
    transcript = await repository.get_or_create_active()
    turns = await repository.list_recent_turns(transcript.id)
    provider = OpenAIProvider(
        api_key=api_key, model=settings.openai_model,
        capability_call_limit=settings.capability_call_limit,
        capability_completion_reserve=settings.capability_completion_reserve,
        capability_policy=capability_runtime.enabled_capabilities, input_token_budget=min(settings.working_context_tokens, settings.openai_context_window),
    )
    instructions = build_model_instructions(await capability_runtime.compact_index_current())
    static_tokens = await provider.count_input_tokens(instructions=instructions, messages=[])
    _, working_policy = await _assemble_working_context(provider, transcript, turns)
    current_tokens = int(working_policy["input_tokens"])
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
    tool_scope_turns = _recent_exchange_turns(turns, 20)
    tool_records = _tool_evidence_records(tool_scope_turns)
    operation_groups: dict[str, dict[str, Any]] = {}
    for record in tool_records:
        operation = str(record["operation"])
        group = operation_groups.setdefault(operation, {
            "messages": [],
            "observations": 0,
            "payload_characters": 0,
            "bands": {"last_10": 0, "exchanges_11_15": 0, "exchanges_16_20": 0},
        })
        group["messages"].append(record["message"])
        group["observations"] += 1
        group["payload_characters"] += int(record["payload_characters"])
        group["bands"][_tool_band(int(record["exchange_age"]))] += 1

    semaphore = asyncio.Semaphore(4)

    async def count_dynamic(messages: list[dict[str, str]]) -> int:
        if not messages:
            return 0
        async with semaphore:
            total = await provider.count_input_tokens(instructions=instructions, messages=messages)
        return max(0, total - static_tokens)

    operation_names = sorted(operation_groups)
    largest_candidates = sorted(
        tool_records, key=lambda item: int(item["payload_characters"]), reverse=True
    )[:8]
    measurement_tasks = [count_dynamic([record["message"] for record in tool_records])]
    measurement_tasks.extend(
        count_dynamic(operation_groups[name]["messages"]) for name in operation_names
    )
    measurement_tasks.extend(count_dynamic([record["message"]]) for record in largest_candidates)
    measurements = await asyncio.gather(*measurement_tasks)

    tool_total_tokens = measurements[0]
    operation_token_values = measurements[1 : 1 + len(operation_names)]
    largest_token_values = measurements[1 + len(operation_names) :]
    tool_operations = []
    for operation, projected_tokens in zip(operation_names, operation_token_values, strict=True):
        group = operation_groups[operation]
        tool_operations.append({
            "operation": operation,
            "observations": group["observations"],
            "projected_tokens": projected_tokens,
            "payload_characters": group["payload_characters"],
            "bands": group["bands"],
        })
    tool_operations.sort(key=lambda item: int(item["projected_tokens"]), reverse=True)

    largest_observations = [
        {
            "operation": record["operation"],
            "phase": record["phase"],
            "exchange_age": record["exchange_age"],
            "projected_tokens": projected_tokens,
            "payload_characters": record["payload_characters"],
        }
        for record, projected_tokens in zip(largest_candidates, largest_token_values, strict=True)
    ]
    largest_observations.sort(key=lambda item: int(item["projected_tokens"]), reverse=True)

    limit_tokens = int(working_policy["token_budget"])
    return {
        "transcript_id": str(transcript.id),
        "static_tokens": static_tokens,
        "current_context_tokens": current_tokens,
        "canonical_transcript_tokens": canonical_tokens,
        "measurement_scope": "Latest 20 owner exchanges, at most 500 canonical turns",
        "limit_tokens": limit_tokens,
        "provider_limit_tokens": settings.openai_context_window,
        "pressure": current_tokens / max(1, limit_tokens),
        "state": _context_pressure_state(current_tokens, limit_tokens),
        "policy": working_policy,
        "summary_present": bool(transcript.context_summary),
        "transcript": _turn_statistics(turns),
        "windows": windows,
        "tool_analysis": {
            "scope_exchanges": min(20, _turn_statistics(tool_scope_turns)["owner_messages"]),
            "observations": len(tool_records),
            "projected_tokens": tool_total_tokens,
            "operations": tool_operations,
            "largest_observations": largest_observations,
        },
    }


@app.get("/api/conversation")
async def conversation(session: Annotated[AsyncSession, Depends(get_session)],
        before_sequence: int | None = Query(default=None, ge=1), limit: int = Query(default=200, ge=1, le=200)):
    repository = TranscriptRepository(session)
    transcript = await repository.get_or_create_active()
    turns = await repository.list_turns(transcript.id, before_sequence=before_sequence, limit=limit)
    await session.commit()
    return {"transcript": transcript, "turns": turns, "next_before_sequence": turns[0].sequence if len(turns) == limit and turns[0].sequence > 1 else None}


def _begin_owner_checkpoint(state, request, owner_turn_id):
    updated = begin_owner_turn(state, request)
    updated["runtime"]["owner_turn_id"] = str(owner_turn_id)
    return updated


@app.post("/api/conversation/stream")
async def stream_conversation(request: ChatRequest):
    text = request.text.strip()
    attachment_paths = [path.strip() for path in request.attachments if path.strip()]
    if attachment_paths:
        await require_capability("atlas.local_storage")
    if not text and not attachment_paths:
        raise HTTPException(status_code=422, detail="Message text or an attachment is required")

    api_key = settings.openai_api_key
    if api_key is None:
        raise HTTPException(status_code=503, detail="OpenAI provider is not configured")

    factory = get_session_factory()
    async with factory() as session:
        repository = TranscriptRepository(session)
        transcript = await repository.get_or_create_active()
        run_intent = text or f"Attached {len(attachment_paths)} local workspace file(s)"
        try:
            run_id = await AuthorityStore(session).create_run(transcript_id=transcript.id, intent=run_intent)
        except ForegroundBusy as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        blocks = [TextBlock(text=text)]
        for path in attachment_paths:
            resource = await asyncio.to_thread(
                LocalStorageService(settings.workspace_root, settings.workspace_display_root).acquire_file, path)
            frozen = await EvidenceStore(session, artifact_store).freeze(resource, provenance={
                "source": "owner_attachment", "run_id": str(run_id), "transcript_id": str(transcript.id), "path": path})
            snapshot = frozen["resource"]
            blocks.append(ArtifactRefBlock(artifact_id=UUID(snapshot["artifact_id"]), filename=snapshot["name"],
                provenance={"source": "owner_attachment", "path": path, "sha256": snapshot["snapshot_sha256"]}))
        await session.execute(update(OwnerAttentionRow).where(
            OwnerAttentionRow.run_id.in_(select(RunRow.id).where(RunRow.transcript_id == transcript.id)),
            OwnerAttentionRow.state == "interrupted", OwnerAttentionRow.resolved.is_(False)
        ).values(resolved=True, resolved_at=datetime.now(UTC)))
        owner_turn = await repository.append_turn(transcript.id, Actor.OWNER, blocks)
        task_state = await repository.mutate_active_task_state(
            transcript.id, lambda state: _begin_owner_checkpoint(state, run_intent, owner_turn.id)
        )
        transcript.active_task_state = task_state
        await session.commit()
        turns = await repository.list_recent_turns(transcript.id)

    try:
        provider = OpenAIProvider(api_key=api_key, model=settings.openai_model, capability_call_limit=settings.capability_call_limit, capability_completion_reserve=settings.capability_completion_reserve, capability_policy=capability_runtime.enabled_capabilities, input_token_budget=min(settings.working_context_tokens, settings.openai_context_window))
        async with maintain_heartbeat(factory, run_id):
            messages = await _prepare_provider_messages(provider, transcript, turns)
    except BaseException:
        await asyncio.shield(interrupt_run(factory, artifact_store, run_id, reason="Context preparation was interrupted; task state was retained."))
        raise
    executor = RunExecutor(factory, capability_runtime, artifact_store, run_id=run_id, transcript_id=transcript.id)
    tool_handler = executor.tool_handler

    provider_evidence_id = None

    async def observation_handler(payload: dict[str, Any]) -> str:
        nonlocal provider_evidence_id
        async with factory() as session:
            provider_evidence_id, _ = await EvidenceStore(session, artifact_store).record(transcript.id,
                operation="provider.openai", phase="observed", detail=payload, run_id=run_id,
                checkpoint=False, trust="external")
            await session.commit()
            return str(provider_evidence_id)

    async def task_state_handler(payload: dict[str, Any]) -> None:
        try:
            delta = TaskStateDelta.model_validate(payload)
        except ValidationError:
            delta = None
        async with factory() as task_session:
            await require_live_run(task_session, run_id)
            repository = TranscriptRepository(task_session)
            if delta is not None:
                state = await repository.mutate_active_task_state(
                    transcript.id, lambda state: merge_semantic_delta(state, delta),
                    expected_task_id=task_state["task_id"],
                )
            else:
                state = await repository.get_active_task_state(transcript.id)
            await EvidenceStore(task_session, artifact_store).record(transcript.id,
                operation="task_state_delta", phase="accepted" if delta else "rejected",
                detail={"delta": payload, "task_id": state.get("task_id"), "revision": state.get("revision"),
                    "provider_evidence_id": str(provider_evidence_id) if provider_evidence_id else None},
                run_id=run_id, checkpoint=False, trust="model")
            await task_session.commit()

    async def checkpoint_reader():
        async with factory() as session:
            state = await TranscriptRepository(session).get_active_task_state(transcript.id)
            return active_task_provider_message(state)

    async def generate() -> AsyncIterator[str]:
        chunks: list[str] = []
        completed = False
        try:
            async with maintain_heartbeat(factory, run_id):
                async for delta in provider.stream_text(
                    instructions=build_model_instructions(await capability_runtime.compact_index_current()),
                    messages=messages, tool_handler=tool_handler,
                    task_state_handler=task_state_handler, observation_handler=observation_handler, checkpoint_reader=checkpoint_reader,
                ):
                    chunks.append(delta)
                    yield json.dumps({"type": "delta", "text": delta}) + "\n"
            answer = "".join(chunks).strip()
            async with factory() as session:
                await require_live_run(session, run_id)
                repository = TranscriptRepository(session)
                if answer:
                    await repository.append_turn(transcript.id, Actor.ATLAS, [TextBlock(text=answer)])
                await AuthorityStore(session).finish_run(run_id)
                await session.commit()
            completed = True
            yield json.dumps({"type": "done", "transcript_id": str(transcript.id)}) + "\n"
        except Exception as exc:  # noqa: BLE001 - persist all foreground interruptions
            yield json.dumps({"type": "error", "message": f"Atlas inference interrupted ({type(exc).__name__}); task state was retained."}) + "\n"
        finally:
            if not completed:
                await asyncio.shield(interrupt_run(factory, artifact_store, run_id,
                    reason="Foreground inference was interrupted. Continue with a new message; prior effects will not be replayed."))

    return StreamingResponse(generate(), media_type="application/x-ndjson")


@app.get("/api/storage/local")
async def local_storage(path: str = ""):
    await require_capability("atlas.local_storage")
    service = LocalStorageService(settings.workspace_root, settings.workspace_display_root)
    try:
        return await asyncio.to_thread(service.list_directory, path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail="Local workspace is not available") from exc
    except NotADirectoryError as exc:
        raise HTTPException(status_code=400, detail="Requested path is not a directory") from exc
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

@app.get("/api/storage/drive")
async def drive_storage(folder_id: str = "root"):
    await require_capability("google.workspace")
    if not settings.gws_configured:
        raise HTTPException(status_code=503, detail="Google Workspace is not configured")
    service = GoogleWorkspaceService(
        settings.gws_command,
        settings.gws_credentials_file,
        settings.gws_config_dir,
        settings.gws_workspace_dir,
    )
    try:
        return await asyncio.to_thread(service.list_drive_folder, folder_id)
    except (RuntimeError, TypeError) as exc:
        raise HTTPException(status_code=502, detail=f"Google Drive read failed: {exc}") from exc


@app.get("/api/project-changes/{change_id}")
async def project_change_download(change_id: UUID):
    service = ProjectChanges(ProjectFolderService(settings.projects_root, settings.projects_display_root, settings.project_checkpoint_root))
    try:
        path = await asyncio.to_thread(service.download, change_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Project change not found") from exc
    return FileResponse(path, media_type="application/zip", filename=f"atlas-change-{change_id}.zip")


@app.get("/api/storage/projects")
async def project_folders(path: str = ""):
    await require_capability("atlas.project_folders")
    service = ProjectFolderService(settings.projects_root, settings.projects_display_root)
    try:
        return await asyncio.to_thread(service.list_directory, path)
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
    await require_capability("github.mcp")
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
    await require_capability("atlas.local_storage")
    data = await file.read()
    if len(data) > 100 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="File exceeds the 100 MB workspace upload limit")
    service = LocalStorageService(settings.workspace_root, settings.workspace_display_root)
    try:
        return await asyncio.to_thread(service.store_file, path, file.filename or "upload", data)
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
    await require_capability("atlas.artifacts")
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


@app.get("/control", include_in_schema=False)
@app.get("/control/", include_in_schema=False)
async def control_page() -> FileResponse:
    index = settings.frontend_dist / "index.html"
    if not index.is_file():
        raise HTTPException(status_code=503, detail="Frontend bundle is not built")
    return FileResponse(index)


if settings.frontend_dist.is_dir():
    app.mount("/", StaticFiles(directory=settings.frontend_dist, html=True), name="frontend")
