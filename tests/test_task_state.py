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


def test_full_checkpoint_preserves_every_admitted_semantic_field() -> None:
    import json
    delta = TaskStateDelta(
        objective='O' * 800,
        constraints=[str(i) + 'C' * 299 for i in range(5)],
        decisions=[{'text': str(i) + 'D' * 399, 'rationale': 'R' * 400} for i in range(5)],
        findings=[str(i) + 'F' * 299 for i in range(5)],
        open_questions=[str(i) + 'Q' * 299 for i in range(5)],
        next_step='N' * 500, status='active',
    )
    state = merge_semantic_delta(new_task_state('owner' * 200), delta)
    for i in range(16):
        state = record_runtime_event(state, operation='demo.effect', phase='prepared',
            evidence_id=str(i), action_id=str(i), arguments={'path': 'p' * 160})
    message = active_task_provider_message(state)
    projected = json.loads(message['content'][message['content'].index('{'):])
    assert projected['semantic'] == state['semantic']
    assert len(projected['runtime']['pending_actions']) == 16
    assert 'model_projection' not in projected


def test_replace_resets_semantics_only() -> None:
    state = record_runtime_event(new_task_state('owner'), operation='demo.send',
        phase='uncertain', evidence_id='evidence', action_id='action')
    state = merge_semantic_delta(state, TaskStateDelta(objective='old', constraints=['old']))
    replaced = merge_semantic_delta(state, TaskStateDelta(replace=True, objective='new'))
    assert replaced['runtime'] == state['runtime']
    assert replaced['task_id'] == state['task_id']
    assert replaced['semantic']['constraints'] == []
    assert replaced['semantic']['objective'] == 'new'


def test_oversized_semantic_delta_is_rejected_instead_of_silently_cut() -> None:
    with pytest.raises(ValidationError):
        TaskStateDelta(constraints=['constraint'] * 6)
    with pytest.raises(ValidationError):
        TaskStateDelta(open_questions=['q' * 301])


def test_empty_delta_does_not_change_task_or_complete_it() -> None:
    state = merge_semantic_delta(new_task_state('owner'), TaskStateDelta(objective='continue'))
    assert merge_semantic_delta(state, TaskStateDelta()) == state
    assert state['status'] == 'active'
    completed = merge_semantic_delta(state, TaskStateDelta(status='complete'))
    assert completed['status'] == 'complete'
    assert begin_owner_turn(completed, 'new')['task_id'] != state['task_id']


def test_large_unresolved_action_set_has_bounded_projection_and_exact_locator():
    import json
    state = new_task_state('Continue')
    state['semantic']['open_questions'] = ['Resolve all pending work']
    for index in range(1000):
        state = record_runtime_event(state, operation='fixture.effect', phase='prepared',
            evidence_id=str(index), action_id=str(index))
    projected = active_task_provider_message(state)
    assert len(state['runtime']['pending_actions']) == 1000
    assert len(projected['content']) < 10000
    assert 'Resolve all pending work' in projected['content']
    assert 'evidence.task.read' in projected['content']
    assert '"pending_actions_count":1000' in projected['content']
    assert json.dumps(state['semantic'])
