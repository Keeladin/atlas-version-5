import pytest
from atlas.runtime.task_state import (
    TaskStateDelta,
    active_task_provider_message,
    begin_owner_turn,
    merge_semantic_delta,
    new_task_state,
    record_runtime_event,
)
from pydantic import ValidationError


def test_owner_turn_starts_and_continues_task_without_replacing_semantics() -> None:
    state = new_task_state("Inspect Atlas context")
    state = merge_semantic_delta(state, TaskStateDelta(objective="Reduce context waste", next_step="Inspect tool evidence"))

    continued = begin_owner_turn(state, "Now check Gmail overhead")

    assert continued["runtime"]["owner_request"] == "Now check Gmail overhead"
    assert continued["runtime"]["owner_turns"] == 2
    assert continued["semantic"]["objective"] == "Reduce context waste"
    assert continued["semantic"]["next_step"] == "Inspect tool evidence"


def test_completed_task_resets_on_next_owner_turn() -> None:
    state = merge_semantic_delta(new_task_state("old"), TaskStateDelta(status="complete"))

    fresh = begin_owner_turn(state, "new objective")

    assert fresh["status"] == "active"
    assert fresh["runtime"]["owner_request"] == "new objective"
    assert fresh["runtime"]["owner_turns"] == 1
    assert fresh["semantic"]["objective"] is None


def test_runtime_event_keeps_refs_but_not_sensitive_arguments() -> None:
    state = record_runtime_event(
        new_task_state("Edit a file"),
        operation="storage.projects.apply",
        phase="succeeded",
        evidence_id="turn-123",
        action_id="action-1",
        arguments={"path": "Atlas/README.md", "content": "SECRET BODY"},
        detail={"status": "succeeded", "output": {"path": "Atlas/README.md", "sha256": "abc"}},
    )

    event = state["runtime"]["recent_events"][-1]
    assert event["evidence_id"] == "turn-123"
    assert event["targets"]["path"] == "Atlas/README.md"
    assert "SECRET BODY" not in str(state)
    assert state["runtime"]["pending_actions"] == []


def test_prepared_action_is_pending_until_terminal_event() -> None:
    state = record_runtime_event(
        new_task_state("Send mail"),
        operation="gmail.message.send",
        phase="prepared",
        evidence_id="turn-1",
        action_id="action-1",
        arguments={"to": "person@example.com", "body": "private body"},
    )
    assert state["runtime"]["pending_actions"][0]["action_id"] == "action-1"

    state = record_runtime_event(
        state,
        operation="gmail.message.send",
        phase="cancelled",
        evidence_id="turn-2",
        action_id="action-1",
    )
    assert state["runtime"]["pending_actions"] == []


def test_model_delta_cannot_write_runtime_owned_fields() -> None:
    with pytest.raises(ValidationError):
        TaskStateDelta.model_validate({"files_modified": ["README.md"], "next_step": "Continue"})


def test_semantic_merge_preserves_runtime_truth() -> None:
    state = record_runtime_event(
        new_task_state("Investigate"),
        operation="storage.projects.acquire",
        phase="succeeded",
        evidence_id="turn-9",
        arguments={"path": "Atlas/backend.py"},
    )
    updated = merge_semantic_delta(
        state,
        TaskStateDelta(
            objective="Fix context growth",
            decisions=[{"text": "Keep the 10-exchange guardrail", "rationale": "Tool evidence is the leak"}],
            next_step="Bound observations",
        ),
    )

    assert updated["runtime"] == state["runtime"]
    assert updated["semantic"]["objective"] == "Fix context growth"
    assert updated["semantic"]["decisions"][0]["rationale"] == "Tool evidence is the leak"


def test_active_task_projects_as_protected_developer_state() -> None:
    message = active_task_provider_message(
        merge_semantic_delta(new_task_state("Do the work"), TaskStateDelta(objective="Finish safely"))
    )

    assert message is not None
    assert message["role"] == "developer"
    assert "Protected active-task checkpoint" in message["content"]
    assert "Finish safely" in message["content"]


def test_active_semantic_delta_requires_concrete_next_step() -> None:
    with pytest.raises(ValidationError):
        TaskStateDelta(status="active")

    delta = TaskStateDelta(status="active", next_step="Run the verification suite")
    assert delta.status == "active"
    assert delta.next_step == "Run the verification suite"
