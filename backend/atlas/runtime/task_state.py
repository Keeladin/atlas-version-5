from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from atlas.runtime.evidence import attach_projection_metadata, bound_model_evidence


class TaskDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=400)
    rationale: str | None = Field(default=None, max_length=400)


class TaskStateDelta(BaseModel):
    """Model-owned semantic changes only; runtime facts are deliberately excluded."""

    model_config = ConfigDict(extra="forbid")

    objective: str | None = Field(default=None, max_length=800)
    constraints: list[str] | None = None
    decisions: list[TaskDecision] | None = None
    findings: list[str] | None = None
    open_questions: list[str] | None = None
    next_step: str | None = Field(default=None, max_length=500)
    status: Literal["active", "complete"] | None = None
    replace: bool = False

    @model_validator(mode="after")
    def active_requires_next_step(self):
        if self.status == "active" and not (self.next_step or "").strip():
            raise ValueError("An active task-state delta requires next_step")
        return self

_MAX_OWNER_REQUEST = 1000
_MAX_LIST = 5
_MAX_EVENTS = 8
_MAX_WORKING_SET = 8
_MAX_PENDING = 8
_MAX_ERRORS = 5
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
    return {
        "version": 1,
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


def begin_owner_turn(state: dict[str, Any] | None, owner_request: str) -> dict[str, Any]:
    if not state or state.get("status") == "complete":
        return new_task_state(owner_request)
    result = deepcopy(state)
    runtime = result.setdefault("runtime", {})
    runtime["owner_request"] = _clip(owner_request, _MAX_OWNER_REQUEST)
    runtime["owner_turns"] = int(runtime.get("owner_turns") or 0) + 1
    result["status"] = "active"
    return result


def _safe_targets(arguments: dict[str, Any], detail: dict[str, Any]) -> dict[str, str]:
    found: dict[str, str] = {}

    def inspect(value: Any) -> None:
        if not isinstance(value, dict):
            return
        for key in _SAFE_KEYS:
            if len(found) >= 4:
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
    if targets:
        working_set = list(runtime.get("working_set") or [])
        working_item = {"operation": operation, **targets, "evidence_id": evidence_id}
        key = json.dumps({k: v for k, v in working_item.items() if k != "evidence_id"}, sort_keys=True)
        deduped = [item for item in working_set if json.dumps({k: v for k, v in item.items() if k != "evidence_id"}, sort_keys=True) != key]
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
    elif action_id and phase in {"succeeded", "failed", "cancelled", "rejected", "uncertain"}:
        pending = [item for item in pending if item.get("action_id") != action_id]
    runtime["pending_actions"] = pending[-_MAX_PENDING:]

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


def merge_semantic_delta(state: dict[str, Any] | None, delta: TaskStateDelta) -> dict[str, Any]:
    result = deepcopy(state) if state else new_task_state()
    if delta.replace:
        owner_request = str(result.get("runtime", {}).get("owner_request") or "")
        result = new_task_state(owner_request)
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
    if delta.status is not None:
        result["status"] = delta.status
        if delta.status == "complete":
            semantic["next_step"] = None
    return result


def active_task_provider_message(state: dict[str, Any] | None) -> dict[str, str] | None:
    if not state or state.get("status") != "active":
        return None
    projected, metadata = bound_model_evidence(state, char_limit=9_000)
    projected = attach_projection_metadata(projected, metadata)
    encoded = json.dumps(projected, ensure_ascii=False, separators=(",", ":"), default=str)
    return {
        "role": "developer",
        "content": (
            "Protected active-task checkpoint. Runtime fields are authoritative facts; semantic fields are the model's compact task meaning. "
            "Keep this task coherent even if older dialogue or tool evidence is omitted. "
            + encoded
        ),
    }
