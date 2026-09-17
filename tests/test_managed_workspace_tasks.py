import pytest
from atlas.actions.authority import AuthorityStore, ProposalIntegrityError
from atlas.config import Settings
from atlas.integrations import workspace_tasks_mcp_server as workspace_mcp
from atlas.persistence.models import ActionRow, OwnerAttentionRow, RunRow, TranscriptRow
from atlas.runtime import managed_tasks_worker as worker
from atlas.runtime.recovery import RunInterrupted, require_live_run
from atlas.runtime.task_state import (
    TaskStateDelta,
    active_task_provider_message,
    merge_semantic_delta,
    new_managed_task_state,
    record_runtime_event,
)
from atlas.transcript.repository import TranscriptRepository
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


def test_material_progress_ignores_poll_evidence_but_tracks_task_change():
    state = _state()
    state["runtime"]["managed_turns"] = 1
    first = worker._progress_signature(state)
    state["runtime"]["managed_turns"] = 2
    assert worker._progress_signature(state) == first

    observed = record_runtime_event(
        state,
        operation="coding.agent.get_status",
        phase="succeeded",
        evidence_id="evidence-progress",
        detail={"output": {"session_id": "12345678-1234-1234-1234-123456789abc"}},
    )
    assert worker._progress_signature(observed) == first

    advanced = merge_semantic_delta(
        observed,
        TaskStateDelta(
            next_step="Run verification",
            current_checkpoint="Verify",
            progress=50,
            status="active",
        ),
    )
    assert worker._progress_signature(advanced) != first


def test_provider_incomplete_is_transient_but_runtime_invariant_failure_is_not():
    assert worker._is_transient_iteration_failure(
        RuntimeError("Provider response did not complete; task state was preserved")
    )
    assert not worker._is_transient_iteration_failure(
        RuntimeError("Managed task transcript disappeared")
    )


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
    assert created["retry_count"] == 0
    assert created["transient_retry_count"] == 0

    listed = await workspace_mcp._list({})
    assert [item["task_id"] for item in listed["items"]] == [created["task_id"]]
    fetched = await workspace_mcp._get({"task_id": created["task_id"]})
    assert fetched["acceptance_criteria"][0]["status"] == "pending"


@pytest.mark.asyncio
async def test_repeated_status_polling_still_reaches_no_progress_stall(
    pg_factory, monkeypatch, tmp_path
):
    monkeypatch.setattr(workspace_mcp, "get_session_factory", lambda: pg_factory)
    monkeypatch.setattr(worker, "get_session_factory", lambda: pg_factory)
    monkeypatch.setenv("ATLAS_MANAGED_TASK_MAX_NO_PROGRESS", "3")
    created = await workspace_mcp._create({
        "objective": "Detect a stuck coding worker",
        "acceptance_criteria": ["Repeated status reads do not masquerade as progress"],
    })
    settings = Settings(
        state_dir=tmp_path / "state",
        artifact_dir=tmp_path / "artifacts",
        database_url=None,
    )
    run_id = await worker._claim_one(settings)
    assert run_id is not None

    async with pg_factory() as session:
        row = (await session.execute(select(TranscriptRow).where(
            TranscriptRow.active_task_state["task_id"].astext == created["task_id"]
        ))).scalar_one()
        initial = worker._progress_signature(dict(row.active_task_state or {}))

    for index in range(3):
        async with pg_factory() as session:
            row = (await session.execute(select(TranscriptRow).where(
                TranscriptRow.active_task_state["task_id"].astext == created["task_id"]
            ).with_for_update())).scalar_one()
            state = record_runtime_event(
                dict(row.active_task_state or {}),
                operation="coding.agent.get_status",
                phase="succeeded",
                evidence_id=f"poll-{index}",
                detail={"output": {"status": "running", "session_id": "same-session"}},
            )
            await TranscriptRepository(session).update_active_task_state(
                row.id, state, expected_revision=row.active_task_revision
            )
            await session.commit()
        await worker._record_iteration_outcome(
            run_id,
            initial_progress_signature=initial,
            succeeded=True,
        )

    async with pg_factory() as session:
        row = (await session.execute(select(TranscriptRow).where(
            TranscriptRow.active_task_state["task_id"].astext == created["task_id"]
        ))).scalar_one()
        runtime = row.active_task_state["runtime"]
        attention = list((await session.execute(
            select(OwnerAttentionRow).where(OwnerAttentionRow.run_id == run_id)
        )).scalars())
    assert runtime["controller_state"] == "stalled"
    assert runtime["retry_count"] == 3
    assert any(item.state == "managed_task_stalled" for item in attention)


@pytest.mark.asyncio
async def test_transient_failure_does_not_consume_no_progress_budget(
    pg_factory, monkeypatch, tmp_path
):
    monkeypatch.setattr(workspace_mcp, "get_session_factory", lambda: pg_factory)
    monkeypatch.setattr(worker, "get_session_factory", lambda: pg_factory)
    monkeypatch.setenv("ATLAS_MANAGED_TASK_POLL_SECONDS", "5")
    created = await workspace_mcp._create({
        "objective": "Survive a temporary provider outage",
        "acceptance_criteria": ["Task resumes without owner Continue"],
    })
    settings = Settings(
        state_dir=tmp_path / "state",
        artifact_dir=tmp_path / "artifacts",
        database_url=None,
    )
    run_id = await worker._claim_one(settings)
    assert run_id is not None

    async with pg_factory() as session:
        row = (await session.execute(select(TranscriptRow).where(
            TranscriptRow.active_task_state["task_id"].astext == created["task_id"]
        ))).scalar_one()
        signature = worker._progress_signature(dict(row.active_task_state or {}))

    await worker._record_iteration_outcome(
        run_id,
        initial_progress_signature=signature,
        succeeded=False,
        failure_kind="transient",
        failure_message="rate limited",
    )

    async with pg_factory() as session:
        row = (await session.execute(select(TranscriptRow).where(
            TranscriptRow.active_task_state["task_id"].astext == created["task_id"]
        ))).scalar_one()
        runtime = row.active_task_state["runtime"]
    assert runtime["controller_state"] == "retrying"
    assert runtime["retry_count"] == 0
    assert runtime["transient_retry_count"] == 1
    assert runtime["next_wake_at"] is not None


@pytest.mark.asyncio
async def test_workspace_resume_resets_both_retry_budgets(pg_factory, monkeypatch):
    monkeypatch.setattr(workspace_mcp, "get_session_factory", lambda: pg_factory)
    created = await workspace_mcp._create({
        "objective": "Reset a genuine stall",
        "acceptance_criteria": ["Both retry budgets are cleared"],
    })
    async with pg_factory() as session:
        row = (await session.execute(select(TranscriptRow).where(
            TranscriptRow.active_task_state["task_id"].astext == created["task_id"]
        ).with_for_update())).scalar_one()
        state = dict(row.active_task_state or {})
        runtime = state.setdefault("runtime", {})
        runtime.update({
            "controller_state": "stalled",
            "retry_count": 3,
            "transient_retry_count": 48,
            "next_wake_at": None,
        })
        await TranscriptRepository(session).update_active_task_state(
            row.id, state, expected_revision=row.active_task_revision
        )
        await session.commit()

    resumed = await workspace_mcp._resume({"task_id": created["task_id"]})
    assert resumed["controller_state"] == "ready"
    assert resumed["retry_count"] == 0
    assert resumed["transient_retry_count"] == 0
    assert resumed["next_wake_at"] is not None


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
async def test_cancelled_task_fences_live_run_before_cleanup(pg_factory, monkeypatch, tmp_path):
    monkeypatch.setattr(workspace_mcp, "get_session_factory", lambda: pg_factory)
    monkeypatch.setattr(worker, "get_session_factory", lambda: pg_factory)
    settings = Settings(
        state_dir=tmp_path / "state",
        artifact_dir=tmp_path / "artifacts",
        database_url=None,
    )
    created = await workspace_mcp._create({
        "objective": "Fence cancellation immediately",
        "acceptance_criteria": ["No effect dispatch after task cancellation"],
    })
    run_id = await worker._claim_one(settings)
    assert run_id is not None

    async with pg_factory() as session:
        row = (await session.execute(select(TranscriptRow).where(
            TranscriptRow.active_task_state["task_id"].astext == created["task_id"]
        ).with_for_update())).scalar_one()
        state = dict(row.active_task_state or {})
        state["status"] = "cancelled"
        state.setdefault("runtime", {})["controller_state"] = "cancelled"
        await TranscriptRepository(session).update_active_task_state(
            row.id, state, expected_revision=row.active_task_revision
        )
        await session.commit()

    async with pg_factory() as session:
        with pytest.raises(RunInterrupted, match="managed task was cancelled"):
            await require_live_run(session, run_id)


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


@pytest.mark.asyncio
async def test_cancelled_managed_task_rejects_late_effect_dispatch(
    pg_factory, monkeypatch, tmp_path
):
    monkeypatch.setattr(workspace_mcp, "get_session_factory", lambda: pg_factory)
    monkeypatch.setattr(worker, "get_session_factory", lambda: pg_factory)
    created = await workspace_mcp._create({
        "objective": "Fence late effects after cancellation",
        "acceptance_criteria": ["No effect begins after cancellation"],
    })
    settings = Settings(
        state_dir=tmp_path / "state",
        artifact_dir=tmp_path / "artifacts",
        database_url=None,
    )
    run_id = await worker._claim_one(settings)
    assert run_id is not None

    async with pg_factory() as session:
        row = (await session.execute(select(TranscriptRow).where(
            TranscriptRow.active_task_state["task_id"].astext == created["task_id"]
        ).with_for_update())).scalar_one()
        state = dict(row.active_task_state or {})
        state["status"] = "cancelled"
        state.setdefault("runtime", {})["controller_state"] = "cancelled"
        await TranscriptRepository(session).update_active_task_state(
            row.id, state, expected_revision=row.active_task_revision
        )
        await session.commit()

    async with pg_factory() as session:
        store = AuthorityStore(session)
        with pytest.raises(ProposalIntegrityError, match="no longer active"):
            await store.begin_automatic_execution(
                run_id=run_id,
                operation="coding.agent.send_turn",
                arguments={"session_id": "s", "prompt": "continue"},
                summary="late effect",
                capability_id="coding.agent",
            )
        await session.rollback()

    async with pg_factory() as session:
        with pytest.raises(ProposalIntegrityError, match="no longer active"):
            await AuthorityStore(session).prepare_proposal(
                run_id=run_id,
                operation="coding.agent.send_turn",
                arguments={"session_id": "s", "prompt": "continue"},
                title="Late proposal",
                capability_id="coding.agent",
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_cancelled_managed_task_rejects_late_proposal_approval(
    pg_factory, monkeypatch, tmp_path
):
    monkeypatch.setattr(workspace_mcp, "get_session_factory", lambda: pg_factory)
    monkeypatch.setattr(worker, "get_session_factory", lambda: pg_factory)
    created = await workspace_mcp._create({
        "objective": "Fence stale approvals after cancellation",
        "acceptance_criteria": ["A stale approval cannot execute"],
    })
    settings = Settings(
        state_dir=tmp_path / "state",
        artifact_dir=tmp_path / "artifacts",
        database_url=None,
    )
    run_id = await worker._claim_one(settings)
    assert run_id is not None

    async with pg_factory() as session:
        action_id = await AuthorityStore(session).prepare_proposal(
            run_id=run_id,
            operation="coding.agent.send_turn",
            arguments={"session_id": "s", "prompt": "continue"},
            title="Continue coding",
            capability_id="coding.agent",
        )
        await session.commit()

    async with pg_factory() as session:
        row = (await session.execute(select(TranscriptRow).where(
            TranscriptRow.active_task_state["task_id"].astext == created["task_id"]
        ).with_for_update())).scalar_one()
        state = dict(row.active_task_state or {})
        state["status"] = "cancelled"
        state.setdefault("runtime", {})["controller_state"] = "cancelled"
        await TranscriptRepository(session).update_active_task_state(
            row.id, state, expected_revision=row.active_task_revision
        )
        await session.commit()

    async with pg_factory() as session:
        action = await session.get(ActionRow, action_id)
        with pytest.raises(ProposalIntegrityError, match="no longer active"):
            await AuthorityStore(session).begin_execution(action)
        await session.rollback()


@pytest.mark.asyncio
async def test_workspace_cancel_finds_reserved_coding_session_and_cleans_proposals(
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
        "objective": "Cancel an in-flight coding start",
        "acceptance_criteria": ["Reserved coding session is stopped"],
    })
    run_id = await worker._claim_one(settings)
    assert run_id is not None
    reserved = "12345678-1234-1234-1234-123456789abc"

    async with pg_factory() as session:
        store = AuthorityStore(session)
        prepared_id = await store.prepare_proposal(
            run_id=run_id,
            operation="coding.agent.send_turn",
            arguments={"session_id": reserved, "prompt": "later"},
            title="Pending coding turn",
            capability_id="coding.agent",
        )
        start_id = await store.begin_automatic_execution(
            run_id=run_id,
            operation="coding.agent.start_session",
            arguments={"session_id": reserved, "repo": "/tmp/repo", "prompt": "start"},
            summary="Start coding",
            capability_id="coding.agent",
        )
        await session.commit()

    seen = []

    async def fake_cancel(session_id):
        seen.append(session_id)
        return {"status": "cancelled", "session_id": session_id}

    monkeypatch.setattr(workspace_mcp, "_cancel_coding_session", fake_cancel)
    cancelled = await workspace_mcp._cancel({
        "task_id": created["task_id"],
        "reason": "Owner cancelled the task",
    })

    assert seen == [reserved]
    assert cancelled["cleanup"]["coding_session_id"] == reserved
    assert str(prepared_id) in cancelled["cleanup"]["cancelled_action_ids"]
    async with pg_factory() as session:
        assert (await session.get(ActionRow, prepared_id)).status == "cancelled"
        assert (await session.get(ActionRow, start_id)).status == "uncertain"
