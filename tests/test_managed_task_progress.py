from atlas.runtime import managed_tasks_worker as worker
from atlas.runtime.task_state import new_managed_task_state, record_runtime_event


def _state():
    return new_managed_task_state(
        title="Progress accounting",
        objective="Detect genuine progress only",
        scope=["Atlas V5"],
        acceptance_criteria=["Repeated failures eventually stall"],
    )


def test_error_evidence_does_not_count_as_material_progress():
    first_failure = record_runtime_event(
        _state(),
        operation="coding.agent.send_turn",
        phase="failed",
        evidence_id="failure-evidence-1",
        detail={"message": "Codex worker exited before completing the turn"},
    )
    signature = worker._progress_signature(first_failure)

    repeated_failure = record_runtime_event(
        first_failure,
        operation="coding.agent.send_turn",
        phase="failed",
        evidence_id="failure-evidence-2",
        detail={"message": "Codex worker exited before completing the turn"},
    )
    assert worker._progress_signature(repeated_failure) == signature

    changed_failure = record_runtime_event(
        repeated_failure,
        operation="coding.agent.send_turn",
        phase="failed",
        evidence_id="failure-evidence-3",
        detail={"message": "Codex worker exited after repository validation failed"},
    )
    assert worker._progress_signature(changed_failure) == signature
