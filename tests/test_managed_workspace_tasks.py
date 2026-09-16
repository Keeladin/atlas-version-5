import pytest
from atlas.config import Settings
from atlas.integrations import workspace_tasks_mcp_server as workspace_mcp
from atlas.persistence.models import OwnerAttentionRow, RunRow, TranscriptRow
from atlas.runtime import managed_tasks_worker as worker
from atlas.runtime.task_state import (
    TaskStateDelta,
    active_task_provider_message,
    merge_semantic_delta,
    new_managed_task_state,
    record_runtime_event,
)
from sqlalchemy import select


def _state():
    return new_managed_task_state(
        title="Implement worker",
        objective="Implement the agreed worker end to end",
        scope=["Only Atlas V5"],
        acceptance_criteria=["Tests pass"],
        checkpoints=["Implement", "Verify"],
        project_id="2fcb0988-326c-4bb7-9ad7-e13e55c3ad46",
    )


def test_managed_completion_requires_runtime_evidence():
    state = _state()
    attempted = merge_semantic_delta(state, TaskStateDelta(status="complete"))
    assert attempted["status"] == "active"
    assert attempted["runtime"]["completion_rejected"]

    observed = record_runtime_event(
        attempted,
        operation="coding.agent.get_result",
        phase="succeeded",
        evidence_id="evidence-1",
        detail={"status": "completed"},
    )
    verified = merge_semantic_delta(observed, TaskStateDelta(
        acceptance_updates=[{
            "id": "A1", "status": "passed", "evidence_refs": ["evidence-1"]
        }],
        progress=100,
        status="complete",
    ))
    assert verified["status"] == "complete"
    assert verified["acceptance_criteria"][0]["status"] == "passed"
    assert verified["progress"]["percent"] == 100


def test_managed_checkpoint_tells_model_runtime_will_continue():
    message = active_task_provider_message(_state())
    assert message is not None
    assert "MANAGED TASK CONTROL" in message["content"]
    assert "runtime, not the owner, will invoke the next Atlas turn" in message["content"]


def test_cancellation_can_recover_coding_session_from_runtime_evidence():
    state = record_runtime_event(
        _state(),
        operation="coding.agent.start_session",
        phase="succeeded",
        evidence_id="evidence-coding",
        detail={"output": {"session_id": "12345678-1234-1234-1234-123456789abc"}},
    )
    assert workspace_mcp._coding_session_id(state) == "12345678-1234-1234-1234-123456789abc"


@pytest.mark.asyncio
async def test_workspace_mcp_persists_and_lists_task(pg_factory, monkeypatch):
    monkeypatch.setattr(workspace_mcp, "get_session_factory", lambda: pg_factory)
    created = await workspace_mcp._create({
        "title": "Workspace task",
        "objective": "Finish the agreed implementation",
        "scope": ["Do not deploy"],
        "acceptance_criteria": ["Backend tests pass", "Review is clean"],
        "checkpoints": ["Implement", "Test", "Review"],
        "workspace_path": "/home/jaco/Projects/Atlas version 5",
    })
    assert created["status"] == "active"
    assert created["controller_state"] == "ready"
    assert created["task_id"]
    assert created["project_id"]
    assert created["scope"][0].startswith("Working directory:")

    listed = await workspace_mcp._list({})
    assert [item["task_id"] for item in listed["items"]] == [created["task_id"]]
    fetched = await workspace_mcp._get({"task_id": created["task_id"]})
    assert fetched["acceptance_criteria"][0]["status"] == "pending"


@pytest.mark.asyncio
async def test_workspace_cancel_stops_controller(pg_factory, monkeypatch):
    monkeypatch.setattr(workspace_mcp, "get_session_factory", lambda: pg_factory)
    created = await workspace_mcp._create({
        "objective": "Cancelable task",
        "acceptance_criteria": ["Never runs after cancellation"],
    })
    cancelled = await workspace_mcp._cancel({
        "task_id": created["task_id"], "reason": "Owner changed direction"
    })
    assert cancelled["status"] == "cancelled"
    assert cancelled["controller_state"] == "cancelled"
    assert cancelled["next_wake_at"] is None
    assert cancelled["cleanup"]["coding_session"] == "not_present"


@pytest.mark.asyncio
async def test_workspace_cancel_interrupts_active_run_without_needs_you(
    pg_factory, monkeypatch, tmp_path
):
    monkeypatch.setattr(workspace_mcp, "get_session_factory", lambda: pg_factory)
    monkeypatch.setattr(worker, "get_session_factory", lambda: pg_factory)
    settings = Settings(
        state_dir=tmp_path / "state",
        artifact_dir=tmp_path / "artifacts",
        database_url=None,
    )
    monkeypatch.setattr(workspace_mcp, "get_settings", lambda: settings)
    created = await workspace_mcp._create({
        "objective": "Cancel while Atlas is working",
        "acceptance_criteria": ["Cancellation stops the active inference"],
    })
    run_id = await worker._claim_one(settings)
    assert run_id is not None

    cancelled = await workspace_mcp._cancel({
        "task_id": created["task_id"], "reason": "Owner cancelled the managed task"
    })

    assert cancelled["status"] == "cancelled"
    assert str(run_id) in cancelled["cleanup"]["interrupted_run_ids"]
    async with pg_factory() as session:
        run = await session.get(RunRow, run_id)
        attention = list((await session.execute(
            select(OwnerAttentionRow).where(OwnerAttentionRow.run_id == run_id)
        )).scalars())
    assert run is not None
    assert run.inference_active is False
    assert run.inference_status == "interrupted"
    assert not any(row.state == "interrupted" for row in attention)


@pytest.mark.asyncio
async def test_worker_claim_is_single_writer(pg_factory, monkeypatch, tmp_path):
    monkeypatch.setattr(workspace_mcp, "get_session_factory", lambda: pg_factory)
    monkeypatch.setattr(worker, "get_session_factory", lambda: pg_factory)
    created = await workspace_mcp._create({
        "title": "Claim me",
        "objective": "Exercise the managed controller",
        "acceptance_criteria": ["One background run owns the turn"],
    })
    settings = Settings(
        state_dir=tmp_path / "state",
        artifact_dir=tmp_path / "artifacts",
        database_url=None,
    )
    first = await worker._claim_one(settings)
    second = await worker._claim_one(settings)
    assert first is not None
    assert second is None

    async with pg_factory() as session:
        run = await session.get(RunRow, first)
        assert run is not None
        assert run.kind == "background"
        assert run.inference_active is True
        row = (await session.execute(select(TranscriptRow).where(
            TranscriptRow.active_task_state["task_id"].astext == created["task_id"]
        ))).scalar_one()
        assert row.active_task_state["runtime"]["controller_state"] == "running"
        assert row.active_task_state["runtime"]["last_worker_run_id"] == str(first)
