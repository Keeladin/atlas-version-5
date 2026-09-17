"""MCP surface for durable Atlas-managed workspace tasks.

This server persists task contracts only. It does not execute model turns and it does not
own authorization; Atlas Control resolves authority before the MCP call is dispatched.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from atlas.actions.authority import AuthorityStore, ProposalIntegrityError
from atlas.artifacts.store import ArtifactStore
from atlas.config import get_settings
from atlas.db import get_session_factory
from atlas.integrations.mcp_socket import MCPSocketClient
from atlas.persistence.models import ActionRow, RunRow, TranscriptRow
from atlas.runtime.recovery import interrupt_run
from atlas.runtime.task_state import new_managed_task_state
from atlas.transcript.repository import TranscriptRepository

PROTOCOL_VERSION = "2025-06-18"
SERVER_VERSION = "0.1.0"
TASK_KIND = "managed_task"
CODING_SOCKET = "/run/atlas-v5/mcp/coding-agent.sock"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _reply(request_id: Any, *, result: dict[str, Any] | None = None,
           error: dict[str, Any] | None = None) -> None:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result or {}
    sys.stdout.write(json.dumps(payload, separators=(",", ":"), default=str) + "\n")
    sys.stdout.flush()


def _tool_result(value: Any, *, error: bool = False) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False, default=str)}],
        "structuredContent": value,
        "isError": error,
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


async def _create(arguments: dict[str, Any]) -> dict[str, Any]:
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


async def _list(arguments: dict[str, Any]) -> dict[str, Any]:
    limit = max(1, min(int(arguments.get("limit") or 20), 100))
    include_terminal = bool(arguments.get("include_terminal", True))
    factory = get_session_factory()
    async with factory() as session:
        rows = list((await session.execute(
            select(TranscriptRow)
            .where(TranscriptRow.kind == TASK_KIND)
            .order_by(TranscriptRow.updated_at.desc(), TranscriptRow.created_at.desc())
            .limit(limit)
        )).scalars())
        items = [_state_projection(row) for row in rows]
        if not include_terminal:
            items = [item for item in items if item.get("status") == "active"]
        return {"items": items}


async def _row_for_task(session, task_id: str, *, lock: bool = False) -> TranscriptRow:
    try:
        UUID(task_id)
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
        raise ValueError(f"Managed task not found: {task_id}")
    return row


async def _get(arguments: dict[str, Any]) -> dict[str, Any]:
    task_id = str(arguments.get("task_id") or "").strip()
    factory = get_session_factory()
    async with factory() as session:
        return _state_projection(await _row_for_task(session, task_id))


async def _cancel(arguments: dict[str, Any]) -> dict[str, Any]:
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
        if state.get("status") == "complete":
            raise ValueError("Completed tasks cannot be cancelled")
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


async def _resume(arguments: dict[str, Any]) -> dict[str, Any]:
    task_id = str(arguments.get("task_id") or "").strip()
    factory = get_session_factory()
    async with factory() as session:
        row = await _row_for_task(session, task_id, lock=True)
        state = dict(row.active_task_state or {})
        if state.get("status") != "active":
            raise ValueError("Only active managed tasks can be resumed")
        runtime = state.setdefault("runtime", {})
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
        await session.commit()
        row = await session.get(TranscriptRow, row.id)
        return _state_projection(row)


TOOLS: dict[str, dict[str, Any]] = {
    "create": {
        "description": "Create a durable managed task only after the owner has agreed the objective, scope, acceptance criteria and execution authority. Before creating it, discover the exact operations needed and include them in authority_grants; Atlas runtime preflights those grants so approved work can continue without repeated permission prompts.",
        "inputSchema": {"type": "object", "properties": {
            "title": {"type": "string", "maxLength": 160},
            "objective": {"type": "string", "minLength": 1, "maxLength": 800},
            "scope": {"type": "array", "items": {"type": "string"}, "maxItems": 16},
            "authority_grants": {"type": "array", "items": {"type": "string", "minLength": 1}, "maxItems": 64},
            "acceptance_criteria": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 32},
            "checkpoints": {"type": "array", "items": {"type": "string"}, "maxItems": 32},
            "workspace_path": {"type": "string", "maxLength": 1000},
            "project_id": {"type": "string"}, "source_chat_id": {"type": "string"},
            "policy_preset": {"type": "string", "maxLength": 120},
            "handoff": {"type": "string", "maxLength": 4000}
        }, "required": ["objective", "acceptance_criteria"], "additionalProperties": False},
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
    },
    "list": {
        "description": "List durable managed workspace tasks and their current progress.",
        "inputSchema": {"type": "object", "properties": {
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            "include_terminal": {"type": "boolean"}
        }, "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    "get": {
        "description": "Read one managed task by task ID, including acceptance, authority grants, checkpoint and controller state.",
        "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"], "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    "cancel": {
        "description": "Cancel a managed task and stop its active Atlas/Codex execution as one lifecycle operation.",
        "inputSchema": {"type": "object", "properties": {
            "task_id": {"type": "string"}, "reason": {"type": "string", "maxLength": 500}
        }, "required": ["task_id"], "additionalProperties": False},
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
    },
    "resume": {
        "description": "Clear a managed task controller stall and make it eligible for automatic continuation again.",
        "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"], "additionalProperties": False},
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
    },
}

HANDLERS = {
    "create": _create,
    "list": _list,
    "get": _get,
    "cancel": _cancel,
    "resume": _resume,
}


def _handle(payload: dict[str, Any]) -> None:
    request_id = payload.get("id")
    method = payload.get("method")
    if method == "initialize":
        _reply(request_id, result={
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "atlas-workspace-tasks-mcp", "version": SERVER_VERSION},
        })
        return
    if method == "notifications/initialized":
        return
    if method == "tools/list":
        _reply(request_id, result={"tools": [{"name": name, **definition} for name, definition in TOOLS.items()]})
        return
    if method == "tools/call":
        params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
        name = str(params.get("name") or "")
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        handler = HANDLERS.get(name)
        if handler is None:
            _reply(request_id, result=_tool_result({"error": f"Unknown tool: {name}"}, error=True))
            return
        try:
            result = asyncio.run(handler(arguments))
        except (OSError, RuntimeError, ValueError) as exc:
            _reply(request_id, result=_tool_result({"error": str(exc), "type": type(exc).__name__}, error=True))
            return
        _reply(request_id, result=_tool_result(result))
        return
    if request_id is not None:
        _reply(request_id, error={"code": -32601, "message": f"Method not found: {method}"})


def main() -> int:
    for line in sys.stdin:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            _handle(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())