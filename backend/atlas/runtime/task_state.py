from __future__ import annotations

import json
from copy import deepcopy
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

TaskText = Annotated[str, Field(min_length=1, max_length=300)]


class TaskDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=400)
    rationale: str | None = Field(default=None, max_length=400)


class TaskAcceptanceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=80)
    status: Literal["pending", "passed", "failed"]
    evidence_refs: list[str] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def passed_requires_evidence(self):
        if self.status == "passed" and not self.evidence_refs:
            raise ValueError("Passed acceptance criteria require runtime evidence_refs")
        return self


class TaskStateDelta(BaseModel):
    """Model-owned semantic changes only; runtime facts are deliberately excluded."""

    model_config = ConfigDict(extra="forbid")

    objective: str | None = Field(default=None, max_length=800)
    constraints: list[TaskText] | None = Field(default=None, max_length=5)
    decisions: list[TaskDecision] | None = Field(default=None, max_length=5)
    findings: list[TaskText] | None = Field(default=None, max_length=5)
    open_questions: list[TaskText] | None = Field(default=None, max_length=5)
    next_step: str | None = Field(default=None, max_length=500)
    progress: int | None = Field(default=None, ge=0, le=100)
    current_checkpoint: str | None = Field(default=None, max_length=120)
    completed_checkpoints: list[str] | None = Field(default=None, max_length=32)
    acceptance_updates: list[TaskAcceptanceUpdate] | None = Field(default=None, max_length=32)
    coding_session_id: str | None = Field(default=None, max_length=120)
    status: Literal["active", "complete"] | None = None
    replace: bool = False

    @model_validator(mode="after")
    def active_requires_next_step(self):
        if self.status == "active" and not (self.next_step or "").strip():
            raise ValueError("An active task-state delta requires next_step")
        return self


_MAX_OWNER_REQUEST = 4000
_MAX_LIST = 5
_MAX_EVENTS = 32
_MAX_WORKING_SET = 16
_MAX_ERRORS = 8
_MAX_AUTHORITY_GRANTS = 64
_SAFE_KEYS = (
    "project",
    "path",
    "source_path",
    "target_path",
    "name",
    "message_id",
    "event_id",
    "folder_id",
    "file_id",
    "sha256",
    "session_id",
    "codex_thread_id",
)


def _clip(value: Any, limit: int = 300) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _bounded_strings(values: list[str] | None, *, limit: int = _MAX_LIST) -> list[str] | None:
    if values is None:
        return None
    result: list[str] = []
    for value in values:
        text = _clip(value)
        if text and text not in result:
            result.append(text)
        if len(result) >= limit:
            break
    return result


def new_task_state(owner_request: str = "") -> dict[str, Any]:
    """Conversation checkpoint. It does not make an ordinary chat a managed job."""
    return {
        "version": 3,
        "mode": "conversation",
        "task_id": str(uuid4()),
        "revision": 0,
        "status": "active",
        "runtime": {
            "owner_request": _clip(owner_request, _MAX_OWNER_REQUEST),
            "owner_turns": 1,
            "recent_events": [],
            "working_set": [],
            "pending_actions": [],
            "errors": [],
        },
        "semantic": {
            "objective": None,
            "constraints": [],
            "decisions": [],
            "findings": [],
            "open_questions": [],
            "next_step": None,
        },
    }


def new_managed_task_state(
    *,
    title: str,
    objective: str,
    scope: list[str],
    acceptance_criteria: list[str],
    checkpoints: list[str] | None = None,
    source_chat_id: str | None = None,
    policy_preset: str | None = None,
    project_id: str | None = None,
    handoff: str = "",
    authority_grants: list[str] | None = None,
) -> dict[str, Any]:
    """Durable execution contract created only after the owner agrees the scope."""
    if not objective.strip():
        raise ValueError("Managed task objective is required")
    if not acceptance_criteria:
        raise ValueError("Managed tasks require at least one acceptance criterion")
    state = new_task_state(handoff or objective)
    state.update({
        "mode": "managed",
        "project_id": project_id or str(uuid4()),
        "project_title": _clip(title or objective, 160),
        "source_chat_id": source_chat_id,
        "policy_preset": policy_preset,
        "authority_grants": _bounded_strings(
            authority_grants, limit=_MAX_AUTHORITY_GRANTS
        ) or [],
        "scope": _bounded_strings(scope, limit=16) or [],
        "acceptance_criteria": [
            {"id": f"A{index}", "text": _clip(text, 500), "status": "pending", "evidence_refs": []}
            for index, text in enumerate(acceptance_criteria, start=1)
            if str(text).strip()
        ],
        "checkpoints": [
            {"id": f"C{index}", "text": _clip(text, 300), "status": "pending"}
            for index, text in enumerate(checkpoints or [], start=1)
            if str(text).strip()
        ],
        "progress": {
            "percent": 0,
            "current_checkpoint": None,
            "completed_checkpoints": [],
            "coding_session_id": None,
            "updates": [],
        },
    })
    state["semantic"]["objective"] = _clip(objective, 800)
    state["semantic"]["constraints"] = _bounded_strings(scope, limit=8) or []
    state["semantic"]["next_step"] = "Begin the agreed task and advance the first checkpoint."
    state["runtime"].update({
        "managed_turns": 0,
        "retry_count": 0,
        "last_progress_at": None,
        "completion_rejected": None,
    })
    if not state["acceptance_criteria"]:
        raise ValueError("Managed tasks require at least one non-empty acceptance criterion")
    return state


def begin_owner_turn(state: dict[str, Any] | None, owner_request: str) -> dict[str, Any]:
    if not state or state.get("status") == "complete":
        result = new_task_state(owner_request)
        if state:
            result["runtime"]["pending_actions"] = deepcopy(
                (state.get("runtime") or {}).get("pending_actions", [])
            )
        return result
    result = deepcopy(state)
    runtime = result.setdefault("runtime", {})
    runtime["owner_request"] = _clip(owner_request, _MAX_OWNER_REQUEST)
    runtime["owner_turns"] = int(runtime.get("owner_turns") or 0) + 1
    result["status"] = "active"
    return result


def remove_resolved_pending_actions(
    state: dict[str, Any] | None, resolved_action_ids: set[str]
) -> dict[str, Any]:
    result = deepcopy(state) if state else new_task_state()
    if not resolved_action_ids:
        return result
    runtime = result.setdefault("runtime", {})
    runtime["pending_actions"] = [
        item for item in (runtime.get("pending_actions") or [])
        if str(item.get("action_id") or "") not in resolved_action_ids
    ]
    return result


def _safe_targets(arguments: dict[str, Any], detail: dict[str, Any]) -> dict[str, str]:
    found: dict[str, str] = {}

    def inspect(value: Any) -> None:
        if not isinstance(value, dict):
            return
        for key in _SAFE_KEYS:
            if len(found) >= 6:
                break
            item = value.get(key)
            if isinstance(item, (str, int)) and str(item).strip() and key not in found:
                found[key] = _clip(item, 160)
        for nested_key in ("output", "resource", "result"):
            nested = value.get(nested_key)
            if isinstance(nested, dict):
                inspect(nested)

    inspect(arguments)
    inspect(detail)
    return found


def record_runtime_event(
    state: dict[str, Any] | None,
    *,
    operation: str,
    phase: str,
    evidence_id: str,
    arguments: dict[str, Any] | None = None,
    detail: dict[str, Any] | None = None,
    action_id: str | None = None,
) -> dict[str, Any]:
    result = deepcopy(state) if state else new_task_state()
    runtime = result.setdefault("runtime", {})
    targets = _safe_targets(arguments or {}, detail or {})
    event = {
        "operation": operation,
        "phase": phase,
        "evidence_id": evidence_id,
        "targets": targets,
    }
    if action_id:
        event["action_id"] = action_id
    events = list(runtime.get("recent_events") or [])
    events.append(event)
    runtime["recent_events"] = events[-_MAX_EVENTS:]
    if result.get("mode") == "managed":
        runtime["last_progress_at"] = evidence_id

    if targets:
        working_set = list(runtime.get("working_set") or [])
        working_item = {"operation": operation, **targets, "evidence_id": evidence_id}
        key = json.dumps(
            {k: v for k, v in working_item.items() if k != "evidence_id"}, sort_keys=True
        )
        deduped = [
            item for item in working_set
            if json.dumps(
                {k: v for k, v in item.items() if k != "evidence_id"}, sort_keys=True
            ) != key
        ]
        deduped.append(working_item)
        runtime["working_set"] = deduped[-_MAX_WORKING_SET:]

    pending = list(runtime.get("pending_actions") or [])
    if action_id and phase in {"prepared", "approval_required", "executing"}:
        pending = [item for item in pending if item.get("action_id") != action_id]
        pending.append({
            "action_id": action_id,
            "operation": operation,
            "phase": phase,
            "evidence_id": evidence_id,
            "targets": targets,
        })
    elif action_id and phase == "uncertain":
        pending = [item for item in pending if item.get("action_id") != action_id]
        pending.append({
            "action_id": action_id,
            "operation": operation,
            "phase": phase,
            "evidence_id": evidence_id,
        })
    elif action_id and phase in {"succeeded", "failed", "cancelled", "rejected"}:
        pending = [item for item in pending if item.get("action_id") != action_id]
    runtime["pending_actions"] = pending

    if phase in {"failed", "uncertain", "unavailable", "forbidden"}:
        message = ""
        source = detail or {}
        if isinstance(source.get("message"), str):
            message = _clip(source["message"], 500)
        errors = list(runtime.get("errors") or [])
        errors.append({
            "operation": operation,
            "phase": phase,
            "evidence_id": evidence_id,
            "message": message,
        })
        runtime["errors"] = errors[-_MAX_ERRORS:]
    return result


def _known_evidence_ids(state: dict[str, Any]) -> set[str]:
    runtime = state.get("runtime") or {}
    return {
        str(item.get("evidence_id"))
        for item in runtime.get("recent_events") or []
        if isinstance(item, dict) and item.get("evidence_id")
    }


def _apply_acceptance_updates(state: dict[str, Any], updates: list[TaskAcceptanceUpdate]) -> None:
    criteria = state.get("acceptance_criteria")
    if not isinstance(criteria, list):
        return
    known = _known_evidence_ids(state)
    by_id = {str(item.get("id")): item for item in criteria if isinstance(item, dict)}
    for update in updates:
        item = by_id.get(update.id)
        if item is None:
            continue
        evidence = [str(ref) for ref in update.evidence_refs if str(ref) in known]
        if update.status == "passed" and not evidence:
            continue
        item["status"] = update.status
        item["evidence_refs"] = evidence


def _all_acceptance_passed(state: dict[str, Any]) -> bool:
    criteria = state.get("acceptance_criteria")
    return bool(criteria) and all(
        isinstance(item, dict) and item.get("status") == "passed" and item.get("evidence_refs")
        for item in criteria
    )


def merge_semantic_delta(state: dict[str, Any] | None, delta: TaskStateDelta) -> dict[str, Any]:
    result = deepcopy(state) if state else new_task_state()
    if delta.replace:
        result["semantic"] = new_task_state()["semantic"]
    semantic = result.setdefault("semantic", {})

    if delta.objective is not None:
        semantic["objective"] = _clip(delta.objective, 800)
    for field_name in ("constraints", "findings", "open_questions"):
        values = _bounded_strings(getattr(delta, field_name))
        if values is not None:
            semantic[field_name] = values
    if delta.decisions is not None:
        decisions = []
        for item in delta.decisions[:_MAX_LIST]:
            decision = {"text": _clip(item.text, 400)}
            if item.rationale:
                decision["rationale"] = _clip(item.rationale, 400)
            decisions.append(decision)
        semantic["decisions"] = decisions
    if delta.next_step is not None:
        semantic["next_step"] = _clip(delta.next_step, 500)

    if result.get("mode") == "managed":
        progress = result.setdefault("progress", {})
        if delta.progress is not None:
            progress["percent"] = delta.progress
        if delta.current_checkpoint is not None:
            progress["current_checkpoint"] = _clip(delta.current_checkpoint, 120)
        if delta.completed_checkpoints is not None:
            completed = _bounded_strings(delta.completed_checkpoints, limit=32) or []
            progress["completed_checkpoints"] = completed
            for checkpoint in result.get("checkpoints") or []:
                if isinstance(checkpoint, dict):
                    checkpoint["status"] = (
                        "complete" if checkpoint.get("id") in completed else checkpoint.get("status", "pending")
                    )
        if delta.coding_session_id is not None:
            progress["coding_session_id"] = _clip(delta.coding_session_id, 120)
        if delta.acceptance_updates:
            _apply_acceptance_updates(result, delta.acceptance_updates)
        result.setdefault("runtime", {})["managed_turns"] = int(
            (result.get("runtime") or {}).get("managed_turns") or 0
        ) + 1

    if delta.status is not None:
        if delta.status == "complete" and result.get("mode") == "managed":
            pending = (result.get("runtime") or {}).get("pending_actions") or []
            if pending or not _all_acceptance_passed(result):
                result["status"] = "active"
                result.setdefault("runtime", {})["completion_rejected"] = (
                    "Completion requires every acceptance criterion to be passed with known runtime evidence and no pending actions."
                )
                if not semantic.get("next_step"):
                    semantic["next_step"] = "Verify the remaining acceptance criteria and continue the task."
            else:
                result["status"] = "complete"
                result.setdefault("runtime", {})["completion_rejected"] = None
                semantic["next_step"] = None
                result.setdefault("progress", {})["percent"] = 100
        else:
            result["status"] = delta.status
            if delta.status == "complete":
                semantic["next_step"] = None
    return result


def active_task_provider_message(state: dict[str, Any] | None) -> dict[str, str] | None:
    if not state or state.get("status") != "active":
        return None
    runtime = state.get("runtime") or {}
    projected: dict[str, Any] = {
        "task_id": state.get("task_id"),
        "revision": state.get("revision", 0),
        "status": state["status"],
        "mode": state.get("mode", "conversation"),
        "semantic": deepcopy(state.get("semantic") or {}),
        "runtime": {
            "owner_request": runtime.get("owner_request"),
            "owner_turn_id": runtime.get("owner_turn_id"),
            "pending_actions": [
                {key: item[key] for key in ("action_id", "operation", "phase", "evidence_id") if key in item}
                for item in runtime.get("pending_actions", [])[:16]
            ],
            "pending_actions_count": len(runtime.get("pending_actions", [])),
            "pending_actions_overflow": ({
                "operation_id": "evidence.task.read",
                "task_id": state.get("task_id"),
                "expected_revision": state.get("revision", 0),
                "message": "Additional unresolved action references remain in the exact task checkpoint; page it before planning effects.",
            } if len(runtime.get("pending_actions", [])) > 16 else None),
            "errors": deepcopy(runtime.get("errors", [])),
            "recent_events": deepcopy(runtime.get("recent_events", [])[-8:]),
            "working_set": deepcopy(runtime.get("working_set", [])[-8:]),
            "completion_rejected": runtime.get("completion_rejected"),
        },
    }
    prefix = (
        "Protected active-task checkpoint. Runtime fields are authoritative facts; semantic fields are the model's compact task meaning. "
        "Keep this task coherent even if older dialogue or tool evidence is omitted. "
    )
    if state.get("mode") == "managed":
        projected.update({
            "project_id": state.get("project_id"),
            "project_title": state.get("project_title"),
            "source_chat_id": state.get("source_chat_id"),
            "policy_preset": state.get("policy_preset"),
            "authority_grants": deepcopy(state.get("authority_grants") or []),
            "scope": deepcopy(state.get("scope") or []),
            "acceptance_criteria": deepcopy(state.get("acceptance_criteria") or []),
            "checkpoints": deepcopy(state.get("checkpoints") or []),
            "progress": deepcopy(state.get("progress") or {}),
        })
        prefix += (
            "MANAGED TASK CONTROL: this is a durable background objective, not a one-turn chat reply. "
            "Continue working without asking the owner again for authority already contained in the agreed scope and central Atlas policy. "
            "Exact operation IDs in authority_grants were approved with this task: the runtime may promote Ask me to Auto for those operations only; a current Control Deny or disabled capability still blocks dispatch. "
            "Do not create a scheduled task merely to continue this work. Delegate coding to the Coding agent capability when appropriate, monitor it, and continue from its result. "
            "At the end of every inference set task_state_delta.status=active with next_step while anything remains, or status=complete only after every acceptance criterion is passed with runtime evidence and no action is pending. "
            "The runtime, not the owner, will invoke the next Atlas turn while status remains active. "
        )
    encoded = json.dumps(projected, ensure_ascii=False, separators=(",", ":"), default=str)
    return {"role": "developer", "content": prefix + encoded}
