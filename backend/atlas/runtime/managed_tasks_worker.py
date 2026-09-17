"""Autonomous continuation worker for durable managed Atlas tasks.

A managed task is a persisted execution contract in a dedicated transcript. This worker
re-enters Atlas while the task is active, lets the ordinary capability/authority runtime
decide every effect, and stops only for completion, a pending owner decision, cancellation,
or a bounded genuine stall.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from openai import APIConnectionError, APIStatusError
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from atlas.actions.authority import AuthorityStore
from atlas.actions.models import ActionStatus, RunKind
from atlas.artifacts.store import ArtifactStore
from atlas.capabilities import AuthorityMode
from atlas.capabilities.factory import build_capability_runtime
from atlas.config import Settings, get_settings
from atlas.db import get_session_factory
from atlas.persistence.models import ActionRow, OwnerAttentionRow, RunRow, TranscriptRow
from atlas.providers import OpenAIProvider
from atlas.registry.service import build_phase0_registry
from atlas.runtime.conversation import (
    build_model_instructions,
    turns_to_provider_messages,
)
from atlas.runtime.execution import RunExecutor
from atlas.runtime.observations import EvidenceStore
from atlas.runtime.recovery import (
    RunInterrupted,
    interrupt_run,
    maintain_heartbeat,
    require_live_run,
)
from atlas.runtime.run_events import append_run_event
from atlas.runtime.task_state import (
    TaskStateDelta,
    active_task_provider_message,
    merge_semantic_delta,
    remove_resolved_pending_actions,
)
from atlas.transcript.models import Actor, TextBlock
from atlas.transcript.repository import TranscriptRepository

logger = logging.getLogger(__name__)
TASK_KIND = "managed_task"
_TRANSIENT_STATUS_CODES = {408, 409, 425, 429}


def _poll_seconds() -> int:
    try:
        return max(5, min(int(os.environ.get("ATLAS_MANAGED_TASK_POLL_SECONDS", "15")), 300))
    except ValueError:
        return 15


def _max_no_progress() -> int:
    try:
        return max(1, min(int(os.environ.get("ATLAS_MANAGED_TASK_MAX_NO_PROGRESS", "3")), 10))
    except ValueError:
        return 3


def _max_transient_failures() -> int:
    try:
        return max(
            1,
            min(int(os.environ.get("ATLAS_MANAGED_TASK_MAX_TRANSIENT_FAILURES", "48")), 200),
        )
    except ValueError:
        return 48


def _transient_retry_delay(retries: int) -> int:
    base = max(15, _poll_seconds())
    exponent = min(max(0, retries - 1), 6)
    return min(900, base * (2**exponent))


def _is_transient_iteration_failure(exc: BaseException) -> bool:
    if isinstance(exc, (APIConnectionError, OperationalError, RunInterrupted, TimeoutError, ConnectionError)):
        return True
    if isinstance(exc, APIStatusError):
        status = int(getattr(exc, "status_code", 0) or 0)
        return status in _TRANSIENT_STATUS_CODES or status >= 500
    return isinstance(exc, RuntimeError) and str(exc).startswith(
        "Provider response did not complete;"
    )


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _progress_signature(state: dict[str, Any]) -> str:
    """Hashable material task state; fresh evidence and changing errors are not progress."""
    runtime = state.get("runtime") if isinstance(state.get("runtime"), dict) else {}
    material = {
        "semantic": state.get("semantic") or {},
        "acceptance_criteria": state.get("acceptance_criteria") or [],
        "checkpoints": state.get("checkpoints") or [],
        "progress": state.get("progress") or {},
        "pending_actions": runtime.get("pending_actions") or [],
        "completion_rejected": runtime.get("completion_rejected"),
    }
    return json.dumps(material, sort_keys=True, separators=(",", ":"), default=str)


async def _reconcile_pending(session, row: TranscriptRow, runtime=None) -> dict[str, Any]:
    state = dict(row.active_task_state or {})
    pending = list((state.get("runtime") or {}).get("pending_actions") or [])
    action_ids: list[UUID] = []
    for item in pending:
        try:
            action_ids.append(UUID(str(item.get("action_id") or "")))
        except (TypeError, ValueError):
            continue
    if not action_ids:
        return state

    actions = list((await session.execute(
        select(ActionRow).where(ActionRow.id.in_(action_ids))
    )).scalars())
    attention_rows = (await session.execute(
        select(OwnerAttentionRow.action_id, OwnerAttentionRow.resolved)
        .where(OwnerAttentionRow.action_id.in_(action_ids))
    )).all()
    attention_seen = {str(action_id) for action_id, _ in attention_rows if action_id is not None}
    attention_open = {
        str(action_id) for action_id, resolved in attention_rows
        if action_id is not None and not resolved
    }
    resolved: set[str] = set()
    for action in actions:
        action_id = str(action.id)
        if (
            runtime is not None
            and action.status == ActionStatus.PREPARED.value
            and action.operation.startswith("coding.agent.")
        ):
            evidence = action.evidence if isinstance(action.evidence, dict) else {}
            proposal = evidence.get("proposal") if isinstance(evidence.get("proposal"), dict) else {}
            arguments = proposal.get("arguments") if isinstance(proposal.get("arguments"), dict) else {}
            try:
                authority = await runtime.resolve_authority(action.operation, arguments)
            except (KeyError, ValueError):
                authority = None
            if authority == AuthorityMode.AUTO:
                await AuthorityStore(session).cancel(action)
                refreshed = await session.get(ActionRow, action.id, populate_existing=True)
                if refreshed is not None:
                    refreshed.evidence = {
                        **(refreshed.evidence or {}),
                        "cancel_reason": "superseded_by_current_auto_authority",
                    }
                resolved.add(action_id)
                continue
        if action.status in {
            ActionStatus.SUCCEEDED.value,
            ActionStatus.FAILED.value,
            ActionStatus.CANCELLED.value,
        }:
            resolved.add(action_id)
        elif action.status == ActionStatus.UNCERTAIN.value:
            acknowledged = bool((action.evidence or {}).get("owner_acknowledged_at"))
            attention_resolved = action_id in attention_seen and action_id not in attention_open
            if acknowledged or attention_resolved:
                resolved.add(action_id)
    if not resolved:
        return state

    repository = TranscriptRepository(session)
    return await repository.mutate_active_task_state(
        row.id, lambda current: remove_resolved_pending_actions(current, resolved)
    )


async def _set_controller_state(
    session,
    row: TranscriptRow,
    *,
    controller_state: str,
    next_wake_at: datetime | None,
    retry_count: int | None = None,
    transient_retry_count: int | None = None,
    run_id: UUID | None = None,
    finished: bool = False,
) -> dict[str, Any]:
    def transform(current: dict[str, Any]) -> dict[str, Any]:
        state = dict(current or {})
        runtime = dict(state.get("runtime") or {})
        runtime["controller_state"] = controller_state
        runtime["next_wake_at"] = _iso(next_wake_at) if next_wake_at else None
        if retry_count is not None:
            runtime["retry_count"] = retry_count
        if transient_retry_count is not None:
            runtime["transient_retry_count"] = transient_retry_count
        if run_id is not None:
            runtime["last_worker_run_id"] = str(run_id)
        if finished:
            runtime["last_worker_finished_at"] = _iso(datetime.now(UTC))
        state["runtime"] = runtime
        return state

    return await TranscriptRepository(session).mutate_active_task_state(row.id, transform)


async def _stall_attention(
    session,
    transcript_id: UUID,
    run_id: UUID,
    state: dict[str, Any],
    *,
    message: str | None = None,
) -> None:
    existing = (await session.execute(
        select(OwnerAttentionRow.id)
        .join(RunRow, OwnerAttentionRow.run_id == RunRow.id)
        .where(
            RunRow.transcript_id == transcript_id,
            OwnerAttentionRow.state == "managed_task_stalled",
            OwnerAttentionRow.resolved.is_(False),
        )
        .limit(1)
    )).scalar_one_or_none()
    if existing is not None:
        return
    semantic = state.get("semantic") or {}
    session.add(OwnerAttentionRow(
        run_id=run_id,
        state="managed_task_stalled",
        title=f"Managed task needs attention: {state.get('project_title') or 'Workspace task'}",
        detail={
            "task_id": state.get("task_id"),
            "project_id": state.get("project_id"),
            "message": message or "Atlas made no durable task progress across the bounded automatic retries.",
            "next_step": semantic.get("next_step"),
            "resume": "Resume the task after correcting the blocker; its exact checkpoint and evidence were retained.",
        },
    ))


async def _claim_one(settings: Settings, runtime=None) -> UUID | None:
    now = datetime.now(UTC)
    factory = get_session_factory()
    async with factory() as session:
        rows = list((await session.execute(
            select(TranscriptRow)
            .where(TranscriptRow.kind == TASK_KIND)
            .order_by(TranscriptRow.updated_at, TranscriptRow.created_at)
            .with_for_update(skip_locked=True)
            .limit(20)
        )).scalars())
        for row in rows:
            state = dict(row.active_task_state or {})
            if state.get("mode") != "managed" or state.get("status") != "active":
                continue
            state = await _reconcile_pending(session, row, runtime)
            runtime_state = state.get("runtime") or {}
            pending = runtime_state.get("pending_actions") or []
            if pending:
                await _set_controller_state(
                    session, row, controller_state="waiting_for_owner", next_wake_at=None
                )
                continue
            if runtime_state.get("controller_state") == "stalled":
                continue
            wake = _parse_time(runtime_state.get("next_wake_at"))
            if wake is not None and wake > now:
                continue
            active = (await session.execute(
                select(RunRow.id).where(
                    RunRow.transcript_id == row.id,
                    RunRow.kind == RunKind.BACKGROUND.value,
                    RunRow.inference_active.is_(True),
                ).limit(1)
            )).scalar_one_or_none()
            if active is not None:
                continue

            semantic = state.get("semantic") or {}
            intent = str(semantic.get("next_step") or semantic.get("objective") or "Continue managed task")
            run_id = await AuthorityStore(session).create_run(
                transcript_id=row.id,
                intent=intent,
                kind=RunKind.BACKGROUND,
            )
            run = await session.get(RunRow, run_id)
            project_id = state.get("project_id")
            try:
                run.workspace_id = UUID(str(project_id)) if project_id else None
            except ValueError:
                run.workspace_id = None
            run.trigger_snapshot = {
                "task_id": state.get("task_id"),
                "project_id": project_id,
                "task_revision": row.active_task_revision,
                "controller": "managed_task_worker",
            }
            await _set_controller_state(
                session, row, controller_state="running", next_wake_at=None, run_id=run_id
            )
            await append_run_event(session, run_id, "accepted", {
                "transcript_id": str(row.id),
                "task_id": state.get("task_id"),
                "project_id": project_id,
                "automatic_continuation": True,
            })
            await session.commit()
            return run_id
    return None


async def _record_iteration_outcome(
    run_id: UUID,
    *,
    initial_progress_signature: str,
    succeeded: bool,
    failure_kind: str | None = None,
    failure_message: str | None = None,
) -> None:
    factory = get_session_factory()
    async with factory() as session:
        run = await session.get(RunRow, run_id)
        if run is None or run.transcript_id is None:
            return
        row = await session.get(TranscriptRow, run.transcript_id, with_for_update=True)
        if row is None:
            return
        state = dict(row.active_task_state or {})
        if state.get("status") != "active":
            await _set_controller_state(
                session, row, controller_state=str(state.get("status") or "complete"),
                next_wake_at=None, retry_count=0, transient_retry_count=0,
                run_id=run_id, finished=True,
            )
            await session.commit()
            return

        runtime_state = state.get("runtime") or {}
        if runtime_state.get("pending_actions"):
            await _set_controller_state(
                session, row, controller_state="waiting_for_owner", next_wake_at=None,
                retry_count=0, transient_retry_count=0, run_id=run_id, finished=True,
            )
            await session.commit()
            return

        progressed = _progress_signature(state) != initial_progress_signature
        no_progress_retries = int(runtime_state.get("retry_count") or 0)
        transient_retries = int(runtime_state.get("transient_retry_count") or 0)

        if not succeeded and failure_kind == "transient":
            transient_retries += 1
            if transient_retries >= _max_transient_failures():
                state = await _set_controller_state(
                    session, row, controller_state="stalled", next_wake_at=None,
                    retry_count=no_progress_retries,
                    transient_retry_count=transient_retries,
                    run_id=run_id, finished=True,
                )
                detail = (
                    f"Atlas exhausted {transient_retries} automatic retries after transient "
                    "provider/runtime failures. This is an execution-availability blocker, not "
                    "a lack-of-progress decision."
                )
                if failure_message:
                    detail += f" Last failure: {failure_message[:300]}"
                await _stall_attention(session, row.id, run_id, state, message=detail)
            else:
                delay = _transient_retry_delay(transient_retries)
                await _set_controller_state(
                    session, row, controller_state="retrying",
                    next_wake_at=datetime.now(UTC) + timedelta(seconds=delay),
                    retry_count=no_progress_retries,
                    transient_retry_count=transient_retries,
                    run_id=run_id, finished=True,
                )
                await append_run_event(session, run_id, "retry_scheduled", {
                    "reason": "transient_provider_or_runtime_failure",
                    "attempt": transient_retries,
                    "delay_seconds": delay,
                })
            await session.commit()
            return

        transient_retries = 0
        no_progress_retries = (
            0 if progressed and succeeded else no_progress_retries + 1
        )
        if no_progress_retries >= _max_no_progress():
            state = await _set_controller_state(
                session, row, controller_state="stalled", next_wake_at=None,
                retry_count=no_progress_retries,
                transient_retry_count=transient_retries,
                run_id=run_id, finished=True,
            )
            await _stall_attention(session, row.id, run_id, state)
        else:
            delay = (
                _poll_seconds()
                if progressed and succeeded
                else min(120, _poll_seconds() * (2 ** max(0, no_progress_retries - 1)))
            )
            await _set_controller_state(
                session, row, controller_state="ready",
                next_wake_at=datetime.now(UTC) + timedelta(seconds=delay),
                retry_count=no_progress_retries,
                transient_retry_count=transient_retries,
                run_id=run_id, finished=True,
            )
        await session.commit()


async def _execute_one(run_id: UUID, settings: Settings, runtime) -> None:
    factory = get_session_factory()
    artifacts = ArtifactStore(settings.artifact_dir)
    initial_progress_signature = ""
    completed = False
    failure_kind: str | None = None
    failure_message: str | None = None
    try:
        async with factory() as session:
            run = await require_live_run(session, run_id)
            if run.transcript_id is None:
                raise RuntimeError("Managed task run lost its transcript")
            transcript_id = run.transcript_id
            row = await session.get(TranscriptRow, transcript_id)
            if row is None:
                raise RuntimeError("Managed task transcript disappeared")
            state = dict(row.active_task_state or {})
            initial_progress_signature = _progress_signature(state)
            if state.get("mode") != "managed" or state.get("status") != "active":
                await AuthorityStore(session).finish_run(run_id)
                await session.commit()
                completed = True
                return
            repository = TranscriptRepository(session)
            turns = await repository.list_turns(transcript_id, limit=60)

        provider = OpenAIProvider(
            api_key=settings.openai_api_key or "",
            model=settings.openai_model,
            capability_call_limit=settings.capability_call_limit,
            capability_completion_reserve=settings.capability_completion_reserve,
            capability_policy=runtime.enabled_capabilities,
            input_token_budget=min(settings.working_context_tokens, settings.openai_context_window),
        )
        executor = RunExecutor(
            factory, runtime, artifacts,
            run_id=run_id, transcript_id=transcript_id, checkpoint=True,
        )
        provider_evidence_id: str | None = None

        async def observation_handler(payload: dict[str, Any]) -> str:
            nonlocal provider_evidence_id
            async with factory() as session:
                evidence_id, _ = await EvidenceStore(session, artifacts).record(
                    transcript_id,
                    operation="provider.openai",
                    phase="observed",
                    detail=payload,
                    run_id=run_id,
                    checkpoint=False,
                    trust="external",
                )
                await session.commit()
                provider_evidence_id = str(evidence_id)
                return provider_evidence_id

        async def task_state_handler(payload: dict[str, Any]) -> None:
            try:
                delta = TaskStateDelta.model_validate(payload)
            except ValidationError:
                delta = None
            async with factory() as session:
                await require_live_run(session, run_id)
                repository = TranscriptRepository(session)
                current = await repository.get_active_task_state(transcript_id)
                if delta is not None:
                    state_after = await repository.mutate_active_task_state(
                        transcript_id,
                        lambda value: merge_semantic_delta(value, delta),
                        expected_task_id=str(current.get("task_id") or ""),
                    )
                else:
                    state_after = current
                await EvidenceStore(session, artifacts).record(
                    transcript_id,
                    operation="task_state_delta",
                    phase="accepted" if delta is not None else "rejected",
                    detail={
                        "delta": payload,
                        "task_id": state_after.get("task_id"),
                        "revision": state_after.get("revision"),
                        "provider_evidence_id": provider_evidence_id,
                    },
                    run_id=run_id,
                    checkpoint=False,
                    trust="model",
                )
                await session.commit()

        async def checkpoint_reader():
            async with factory() as session:
                row = await session.get(TranscriptRow, transcript_id, with_for_update=True)
                if row is None:
                    return None
                await _reconcile_pending(session, row)
                await session.commit()
                state = await TranscriptRepository(session).get_active_task_state(transcript_id)
                return active_task_provider_message(state)

        messages = turns_to_provider_messages(turns, owner_timezone=settings.owner_timezone)
        checkpoint = active_task_provider_message(state)
        if checkpoint is not None:
            messages.insert(0, checkpoint)
        messages.append({
            "role": "user",
            "content": (
                "Automatic managed-task continuation turn. This message grants no new authority. "
                "Use only the durable task contract and current Atlas Control policy. Reconcile the "
                "latest checkpoint, execute the next useful step, and revalidate any previously reported "
                "blocker with an available safe read operation before carrying that blocker forward. "
                "Delegate coding/review to the Coding agent capability when appropriate, and leave a "
                "truthful task_state_delta for the next turn."
            ),
        })

        await append_run_event_for_worker(run_id, "running", {})
        chunks: list[str] = []
        async with maintain_heartbeat(factory, run_id):
            async for text in provider.stream_text(
                instructions=build_model_instructions(
                    await runtime.compact_index_current(), owner_timezone=settings.owner_timezone
                ),
                messages=messages,
                tool_handler=executor.tool_handler,
                task_state_handler=task_state_handler,
                observation_handler=observation_handler,
                checkpoint_reader=checkpoint_reader,
            ):
                chunks.append(text)

        answer = "".join(chunks).strip()
        async with factory() as session:
            await require_live_run(session, run_id)
            if answer:
                await TranscriptRepository(session).append_turn(
                    transcript_id, Actor.ATLAS, [TextBlock(text=answer)]
                )
            await AuthorityStore(session).finish_run(run_id)
            await append_run_event(session, run_id, "completed", {
                "transcript_id": str(transcript_id),
                "automatic_continuation": True,
            })
            await session.commit()
        completed = True
    except asyncio.CancelledError:
        failure_kind = "transient"
        failure_message = "Managed-task worker was cancelled before the turn completed"
        raise
    except Exception as exc:
        failure_kind = "transient" if _is_transient_iteration_failure(exc) else "execution"
        failure_message = str(exc)
        logger.exception("Managed task iteration failed: %s", run_id)
    finally:
        if not completed:
            with suppress(Exception):
                await asyncio.shield(interrupt_run(
                    factory, artifacts, run_id,
                    reason="Managed-task inference stopped before a durable completion; automatic recovery will use the retained checkpoint.",
                ))
        with suppress(Exception):
            await _record_iteration_outcome(
                run_id,
                initial_progress_signature=initial_progress_signature,
                succeeded=completed,
                failure_kind=failure_kind,
                failure_message=failure_message,
            )


async def append_run_event_for_worker(run_id: UUID, event_type: str, payload: dict[str, Any]) -> None:
    async with get_session_factory()() as session:
        await append_run_event(session, run_id, event_type, payload)
        await session.commit()


async def run_once(settings: Settings, runtime) -> int:
    run_id = await _claim_one(settings, runtime)
    if run_id is None:
        return 0
    await _execute_one(run_id, settings, runtime)
    return 1


async def worker_loop(settings: Settings) -> None:
    registry = build_phase0_registry(settings)
    runtime = build_capability_runtime(settings, registry)
    logger.info("Managed-task worker started with %d operations", len(runtime.operations()))
    while True:
        try:
            count = await run_once(settings, runtime)
        except Exception:
            logger.exception("Managed-task controller poll failed")
            count = 0
        await asyncio.sleep(1 if count else _poll_seconds())


def main() -> int:
    settings = get_settings()
    if settings.openai_api_key is None:
        raise SystemExit("OpenAI provider is not configured")
    logging.basicConfig(level=logging.INFO)
    asyncio.run(worker_loop(settings))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
