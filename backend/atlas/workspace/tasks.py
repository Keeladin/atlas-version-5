"""Shared durable task contracts and lifecycle for MCP and authenticated owner REST.

Callers enforce authority. Model lifecycle operations remain non-delegable in the
capability runtime; the authenticated owner can operate the same service directly.
"""
from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError

from atlas.actions.authority import AuthorityStore, ProposalIntegrityError
from atlas.artifacts.store import ArtifactStore
from atlas.config import get_settings
from atlas.db import get_session_factory
from atlas.integrations.mcp_socket import MCPSocketClient
from atlas.persistence.models import (
    ActionRow,
    OwnerAttentionRow,
    RunRow,
    TranscriptRow,
    TurnRow,
)
from atlas.runtime.recovery import interrupt_run
from atlas.runtime.task_state import new_managed_task_state
from atlas.transcript.repository import TranscriptRepository

TASK_KIND = "managed_task"
CODING_SOCKET = "/run/atlas-v5/mcp/coding-agent.sock"


class TaskNotFound(ValueError):
    """The requested managed task does not exist."""


class TaskConflict(ValueError):
    """The lifecycle operation conflicts with the current task state."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


async def _resolve_stall_attention(session, transcript_id: UUID) -> None:
    await session.execute(update(OwnerAttentionRow).where(
        OwnerAttentionRow.run_id.in_(select(RunRow.id).where(RunRow.transcript_id == transcript_id)),
        OwnerAttentionRow.state == "managed_task_stalled",
        OwnerAttentionRow.resolved.is_(False),
    ).values(resolved=True, resolved_at=datetime.now(UTC)))


def _activity_executor(operation: str) -> str:
    if operation.startswith("coding.agent."):
        return "Codex"
    if operation in {"task_state_delta", "provider.openai"}:
        return "Atlas"
    return "Atlas tool"


def _activity_summary(operation: str, phase: str, detail: dict[str, Any] | None = None) -> str:
    labels = {
        "coding.agent.start_session": "Started Codex work",
        "coding.agent.get_status": "Checked Codex progress",
        "coding.agent.get_result": "Read Codex result",
        "coding.agent.cancel_session": "Stopped Codex session",
        "storage.projects.status": "Checked repository status",
        "storage.projects.diff": "Inspected repository changes",
        "evidence.task.read": "Read task evidence",
        "task_state_delta": "Updated managed-task state",
    }
    if operation == "task_state_delta" and isinstance(detail, dict):
        delta = detail.get("delta")
        if isinstance(delta, dict) and isinstance(delta.get("next_step"), str) and delta["next_step"].strip():
            return f"Updated plan: {_clip_activity(delta['next_step'], 180)}"
    return labels.get(operation, operation.replace(".", " · ").replace("_", " ")) + f" · {phase.replace('_', ' ')}"


def _clip_activity(value: Any, limit: int = 180) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


async def _live_projection(session, row: TranscriptRow) -> dict[str, Any]:
    state = dict(row.active_task_state or {})
    semantic = state.get("semantic") or {}
    runtime = state.get("runtime") or {}
    progress = state.get("progress") or {}
    safe_events = {
        str(item.get("evidence_id")): item
        for item in runtime.get("recent_events") or []
        if isinstance(item, dict) and item.get("evidence_id")
    }
    turns = list((await session.execute(
        select(TurnRow).where(TurnRow.transcript_id == row.id, TurnRow.deleted_at.is_(None))
        .order_by(TurnRow.sequence.desc()).limit(48)
    )).scalars())
    activity: list[dict[str, Any]] = []
    for turn in turns:
        if len(activity) >= 12:
            break
        if turn.actor == "atlas":
            text = " ".join(
                str(block.get("text") or "")
                for block in (turn.blocks or [])
                if isinstance(block, dict) and block.get("type") == "text"
            ).strip()
            if text:
                activity.append({
                    "timestamp": turn.created_at.isoformat() if turn.created_at else None,
                    "executor": "Atlas",
                    "operation": "managed_turn",
                    "phase": "completed",
                    "summary": "Atlas completed a managed turn",
                    "detail": _clip_activity(text),
                    "evidence_id": str(turn.id),
                    "targets": {},
                })
            continue
        if turn.actor != "tool":
            continue
        block = next((
            item for item in (turn.blocks or [])
            if isinstance(item, dict) and item.get("type") == "tool_observation"
        ), None)
        if not isinstance(block, dict):
            continue
        operation = str(block.get("operation") or "tool")
        phase = str(block.get("phase") or "observed")
        if operation == "provider.openai":
            continue
        detail = block.get("detail") if isinstance(block.get("detail"), dict) else {}
        safe = safe_events.get(str(turn.id), {})
        activity.append({
            "timestamp": turn.created_at.isoformat() if turn.created_at else None,
            "executor": _activity_executor(operation),
            "operation": operation,
            "phase": phase,
            "summary": _activity_summary(operation, phase, detail),
            "detail": None,
            "evidence_id": str(turn.id),
            "targets": safe.get("targets") if isinstance(safe.get("targets"), dict) else {},
        })

    latest_run = (await session.execute(
        select(RunRow).where(RunRow.transcript_id == row.id, RunRow.kind == "background")
        .order_by(RunRow.created_at.desc()).limit(1)
    )).scalar_one_or_none()
    controller_state = str(runtime.get("controller_state") or "ready")
    worker_state = "active" if latest_run is not None and latest_run.inference_active else controller_state
    last_activity_at = next((item["timestamp"] for item in activity if item.get("timestamp")), None)
    if last_activity_at is None and latest_run is not None and latest_run.heartbeat_at is not None:
        last_activity_at = latest_run.heartbeat_at.isoformat()
    current_activity = str(semantic.get("next_step") or "").strip()
    if controller_state == "waiting_for_owner":
        current_activity = "Waiting for an owner decision."
    elif controller_state == "stalled":
        current_activity = current_activity or "Task is stalled and needs owner intervention."
    elif controller_state == "retrying":
        current_activity = current_activity or "Retrying the current managed step."
    elif worker_state == "active":
        current_activity = current_activity or "Atlas is executing the current managed turn."
    else:
        current_activity = current_activity or "Queued for the next managed turn."
    executor = "Atlas + Codex" if str(progress.get("coding_session_id") or "").strip() else "Atlas"
    return {
        "worker_state": worker_state,
        "current_activity": current_activity,
        "executor": executor,
        "last_activity_at": last_activity_at,
        "heartbeat_at": latest_run.heartbeat_at.isoformat() if latest_run is not None and latest_run.heartbeat_at else None,
        "run_id": str(latest_run.id) if latest_run is not None else None,
        "recent_activity": activity,
    }


def _state_projection(row: TranscriptRow) -> dict[str, Any]:
    state = dict(row.active_task_state or {})
    semantic = state.get("semantic") or {}
    runtime = state.get("runtime") or {}
    progress = state.get("progress") or {}
    return {
        "transcript_id": str(row.id),
        "task_id": state.get("task_id"),
        "project_id": state.get("project_id"),
        "title": state.get("project_title") or row.title,
        "status": state.get("status"),
        "controller_state": runtime.get("controller_state", "ready"),
        "objective": semantic.get("objective"),
        "scope": state.get("scope") or [],
        "authority_grants": state.get("authority_grants") or [],
        "acceptance_criteria": state.get("acceptance_criteria") or [],
        "checkpoints": state.get("checkpoints") or [],
        "progress": progress,
        "next_step": semantic.get("next_step"),
        "findings": semantic.get("findings") or [],
        "completion_rejected": runtime.get("completion_rejected"),
        "cancel_reason": runtime.get("cancel_reason"),
        "pending_actions": runtime.get("pending_actions") or [],
        "retry_count": int(runtime.get("retry_count") or 0),
        "transient_retry_count": int(runtime.get("transient_retry_count") or 0),
        "next_wake_at": runtime.get("next_wake_at"),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _strings(value: Any, *, maximum: int) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
        if len(result) >= maximum:
            break
    return result


def _coding_session_id(state: dict[str, Any]) -> str | None:
    progress = state.get("progress") if isinstance(state.get("progress"), dict) else {}
    explicit = str(progress.get("coding_session_id") or "").strip()
    if explicit:
        return explicit
    runtime = state.get("runtime") if isinstance(state.get("runtime"), dict) else {}
    for event in reversed(runtime.get("recent_events") or []):
        if not isinstance(event, dict):
            continue
        if event.get("operation") != "coding.agent.start_session" or event.get("phase") != "succeeded":
            continue
        targets = event.get("targets") if isinstance(event.get("targets"), dict) else {}
        session_id = str(targets.get("session_id") or "").strip()
        if session_id:
            return session_id
    return None


async def _coding_session_id_from_actions(session, transcript_id: UUID) -> str | None:
    rows = list((await session.execute(
        select(ActionRow)
        .join(RunRow, ActionRow.run_id == RunRow.id)
        .where(
            RunRow.transcript_id == transcript_id,
            ActionRow.operation == "coding.agent.start_session",
        )
        .order_by(ActionRow.updated_at.desc())
        .limit(20)
    )).scalars())
    for action in rows:
        evidence = action.evidence if isinstance(action.evidence, dict) else {}
        arguments = evidence.get("arguments")
        if not isinstance(arguments, dict):
            proposal = evidence.get("proposal")
            arguments = proposal.get("arguments") if isinstance(proposal, dict) else {}
        session_id = str((arguments or {}).get("session_id") or "").strip()
        if session_id:
            return session_id
    return None


async def _cancel_coding_session(session_id: str) -> dict[str, Any]:
    path = Path(os.environ.get("ATLAS_CODING_MCP_SOCKET", CODING_SOCKET))
    client = MCPSocketClient(path, timeout=15, connect_timeout=2)
    result = await asyncio.to_thread(client.call_tool, "cancel_session", {"session_id": session_id})
    return result if isinstance(result, dict) else {"result": result}


async def create_task(arguments: dict[str, Any]) -> dict[str, Any]:
    title = str(arguments.get("title") or "").strip()
    objective = str(arguments.get("objective") or "").strip()
    acceptance = _strings(arguments.get("acceptance_criteria"), maximum=32)
    scope = _strings(arguments.get("scope"), maximum=16)
    checkpoints = _strings(arguments.get("checkpoints"), maximum=32)
    authority_grants = _strings(arguments.get("authority_grants"), maximum=64)
    workspace_path = str(arguments.get("workspace_path") or "").strip()
    if workspace_path:
        scope_line = f"Working directory: {workspace_path}"
        if scope_line not in scope:
            scope.insert(0, scope_line)
    if not title:
        title = objective[:160] or "Managed task"
    if not objective:
        raise ValueError("objective is required")
    if not acceptance:
        raise ValueError("acceptance_criteria must contain at least one item")

    source_chat_id = str(arguments.get("source_chat_id") or "").strip() or None
    project_id = str(arguments.get("project_id") or "").strip() or None
    policy_preset = str(arguments.get("policy_preset") or "").strip() or None
    handoff = str(arguments.get("handoff") or "").strip()
    factory = get_session_factory()
    async with factory() as session:
        repository = TranscriptRepository(session)
        transcript = await repository.create(kind=TASK_KIND, title=title)
        state = new_managed_task_state(
            title=title,
            objective=objective,
            scope=scope,
            acceptance_criteria=acceptance,
            checkpoints=checkpoints,
            source_chat_id=source_chat_id,
            policy_preset=policy_preset,
            project_id=project_id,
            handoff=handoff,
            authority_grants=authority_grants,
        )
        state["workspace_path"] = workspace_path or None
        runtime = state.setdefault("runtime", {})
        runtime.update({
            "controller_state": "ready",
            "next_wake_at": _now(),
            "retry_count": 0,
            "transient_retry_count": 0,
            "last_worker_run_id": None,
            "last_worker_finished_at": None,
        })
        await repository.mutate_active_task_state(transcript.id, lambda _current: state)
        row = await session.get(TranscriptRow, transcript.id)
        await session.commit()
        return _state_projection(row)


async def list_tasks(arguments: dict[str, Any]) -> dict[str, Any]:
    limit = max(1, min(int(arguments.get("limit") or 20), 100))
    offset = int(arguments.get("offset") or 0)
    status = arguments.get("status", "all" if arguments.get("include_terminal", True) else "active")
    if status not in {"all", "active", "terminal"} or offset < 0:
        raise ValueError("Invalid task status filter or offset")
    query = select(TranscriptRow).where(TranscriptRow.kind == TASK_KIND)
    task_status = TranscriptRow.active_task_state["status"].astext
    if status == "active":
        query = query.where(task_status == "active")
    elif status == "terminal":
        query = query.where(task_status.in_(("complete", "cancelled")))
    factory = get_session_factory()
    async with factory() as session:
        rows = list((await session.execute(
            query.order_by(TranscriptRow.updated_at.desc(), TranscriptRow.id.desc())
            .offset(offset).limit(limit + 1)
        )).scalars())
        return {
            "items": [_state_projection(row) for row in rows[:limit]],
            "next_offset": offset + limit if len(rows) > limit else None,
        }


async def _row_for_task(session, task_id: str, *, lock: bool = False) -> TranscriptRow:
    try:
        task_id = str(UUID(task_id))
    except (TypeError, ValueError) as exc:
        raise ValueError("task_id must be a UUID") from exc
    query = select(TranscriptRow).where(
        TranscriptRow.kind == TASK_KIND,
        TranscriptRow.active_task_state["task_id"].astext == task_id,
    )
    if lock:
        query = query.with_for_update()
    row = (await session.execute(query)).scalar_one_or_none()
    if row is None:
        raise TaskNotFound(f"Managed task not found: {task_id}")
    return row


async def get_task(arguments: dict[str, Any]) -> dict[str, Any]:
    task_id = str(arguments.get("task_id") or "").strip()
    factory = get_session_factory()
    async with factory() as session:
        row = await _row_for_task(session, task_id)
        result = _state_projection(row)
        result["live"] = await _live_projection(session, row)
        return result


async def cancel_task(arguments: dict[str, Any]) -> dict[str, Any]:
    task_id = str(arguments.get("task_id") or "").strip()
    reason = str(arguments.get("reason") or "Cancelled by owner direction").strip()[:500]
    factory = get_session_factory()
    active_run_ids: list[UUID] = []
    prepared_action_ids: list[UUID] = []
    coding_session_id: str | None = None
    transcript_id: UUID | None = None

    # Commit the terminal task state first. From this point the runtime contract
    # says no new managed work may be dispatched even if cleanup needs recovery.
    async with factory() as session:
        row = await _row_for_task(session, task_id, lock=True)
        transcript_id = row.id
        state = dict(row.active_task_state or {})
        if state.get("status") != "active":
            raise TaskConflict("Only active managed tasks can be cancelled")
        coding_session_id = _coding_session_id(state)
        if not coding_session_id:
            coding_session_id = await _coding_session_id_from_actions(session, row.id)
        prepared_action_ids = list((await session.execute(
            select(ActionRow.id)
            .join(RunRow, ActionRow.run_id == RunRow.id)
            .where(
                RunRow.transcript_id == row.id,
                ActionRow.status == "prepared",
            )
        )).scalars())
        active_run_ids = list((await session.execute(
            select(RunRow.id).where(
                RunRow.transcript_id == row.id,
                RunRow.kind == "background",
                RunRow.inference_active.is_(True),
            )
        )).scalars())
        state["status"] = "cancelled"
        state.setdefault("semantic", {})["next_step"] = None
        runtime = state.setdefault("runtime", {})
        runtime.update({
            "controller_state": "cancelled",
            "next_wake_at": None,
            "pending_actions": [],
            "cancelled_at": _now(),
            "cancel_reason": reason,
        })
        await TranscriptRepository(session).update_active_task_state(
            row.id, state, expected_revision=row.active_task_revision
        )
        await _resolve_stall_attention(session, row.id)
        await session.commit()

    cleanup: dict[str, Any] = {
        "coding_session_id": coding_session_id,
        "coding_session": "not_present" if not coding_session_id else "pending",
        "cancelled_action_ids": [],
        "interrupted_run_ids": [],
        "warnings": [],
    }
    for action_id in prepared_action_ids:
        async with factory() as action_session:
            action = await action_session.get(ActionRow, action_id)
            if action is None or action.status != "prepared":
                continue
            try:
                await AuthorityStore(action_session).cancel(action)
                await action_session.commit()
                cleanup["cancelled_action_ids"].append(str(action_id))
            except ProposalIntegrityError as exc:
                await action_session.rollback()
                cleanup["warnings"].append(
                    f"Prepared action {action_id} cleanup raced with another decision: {exc}"
                )

    if coding_session_id:
        try:
            coding_result = await _cancel_coding_session(coding_session_id)
            cleanup["coding_session"] = coding_result.get("status") or "cancelled"
        except (OSError, RuntimeError, ValueError) as exc:
            cleanup["coding_session"] = "cleanup_failed"
            cleanup["warnings"].append(f"Coding session cleanup failed: {exc}")

    if active_run_ids:
        artifacts = ArtifactStore(get_settings().artifact_dir)
        for run_id in active_run_ids:
            try:
                await interrupt_run(
                    factory,
                    artifacts,
                    run_id,
                    reason=f"Managed task cancelled: {reason}",
                )
                cleanup["interrupted_run_ids"].append(str(run_id))
            except (OSError, RuntimeError, ValueError, SQLAlchemyError) as exc:
                cleanup["warnings"].append(f"Run {run_id} cleanup failed: {exc}")

    async with factory() as session:
        row = await session.get(TranscriptRow, transcript_id)
        if row is None:
            raise RuntimeError("Managed task disappeared after cancellation")
        result = _state_projection(row)
    result["cleanup"] = cleanup
    return result


async def resume_task(arguments: dict[str, Any]) -> dict[str, Any]:
    task_id = str(arguments.get("task_id") or "").strip()
    factory = get_session_factory()
    async with factory() as session:
        row = await _row_for_task(session, task_id, lock=True)
        state = dict(row.active_task_state or {})
        if state.get("status") != "active":
            raise TaskConflict("Only active managed tasks can be resumed")
        runtime = state.setdefault("runtime", {})
        if runtime.get("controller_state") != "stalled":
            raise TaskConflict("Only stalled managed tasks can be resumed")
        runtime.update({
            "controller_state": "ready",
            "retry_count": 0,
            "transient_retry_count": 0,
            "next_wake_at": _now(),
            "completion_rejected": runtime.get("completion_rejected"),
        })
        await TranscriptRepository(session).update_active_task_state(
            row.id, state, expected_revision=row.active_task_revision
        )
        await _resolve_stall_attention(session, row.id)
        await session.commit()
        row = await session.get(TranscriptRow, row.id)
        return _state_projection(row)
