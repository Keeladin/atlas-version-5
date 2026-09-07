from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from atlas.actions.authority import AuthorityStore
from atlas.actions.models import ActionStatus, RunStatus
from atlas.persistence.models import ActionRow, OwnerAttentionRow, RunRow
from atlas.runtime.conversation import context_turns, turns_to_provider_messages
from atlas.schedules.service import ScheduleService, next_run
from atlas.storage import ProjectFolderService
from atlas.transcript.models import Actor, TextBlock, ToolObservationBlock, Turn


class _Result:
    def __init__(self, *, one=None, many=None, rowcount=0):
        self._one = one
        self._many = many or []
        self.rowcount = rowcount

    def scalar_one_or_none(self):
        return self._one

    def scalars(self):
        return self

    def all(self):
        return self._many


class _Session:
    def __init__(self, action=None, run=None, results=()):
        self.action = action
        self.run = run
        self.results = list(results)
        self.added = []
        self.deleted = []

    async def get(self, model, key):
        from atlas.persistence.models import ActionRow, RunRow
        if model is ActionRow:
            return self.action
        if model is RunRow:
            return self.run
        return None

    async def execute(self, statement):
        return self.results.pop(0)

    async def flush(self):
        return None

    async def delete(self, value):
        self.deleted.append(value)

    def add(self, value):
        self.added.append(value)


def _executing_action() -> ActionRow:
    return ActionRow(
        id=uuid4(), run_id=uuid4(), operation="gmail.message.send", target_hash="a" * 64,
        status=ActionStatus.EXECUTING.value, execution_started_at=datetime.now(UTC), evidence={"summary": "Send mail"},
    )


@pytest.mark.asyncio
async def test_success_resolves_attention_and_finishes_run() -> None:
    action = _executing_action()
    attention = OwnerAttentionRow(action_id=action.id, run_id=action.run_id, state="approval_required", title="Send", detail={}, resolved=False)
    run = RunRow(id=action.run_id, kind="foreground", status=RunStatus.WAITING_FOR_OWNER.value)
    session = _Session(action, run, [_Result(one=attention)])

    await AuthorityStore(session).complete(action.id, status=ActionStatus.SUCCEEDED, result={"ok": True})

    assert action.status == ActionStatus.SUCCEEDED.value
    assert attention.resolved is True
    assert attention.resolved_at is not None
    assert run.status == RunStatus.SUCCEEDED.value
    assert run.finished_at is not None


@pytest.mark.asyncio
async def test_failure_stays_distinct_from_uncertain() -> None:
    action = _executing_action()
    run = RunRow(id=action.run_id, kind="foreground", status=RunStatus.RUNNING.value)
    session = _Session(action, run, [_Result(one=None)])

    await AuthorityStore(session).complete(action.id, status=ActionStatus.FAILED, result={"failure_phase": "before_dispatch"})

    assert action.status == ActionStatus.FAILED.value
    assert run.status == RunStatus.FAILED.value
    assert not any(isinstance(item, OwnerAttentionRow) for item in session.added)


@pytest.mark.asyncio
async def test_uncertain_reuses_attention_and_preserves_external_id() -> None:
    action = _executing_action()
    attention = OwnerAttentionRow(action_id=action.id, run_id=action.run_id, state="approval_required", title="Send", detail={}, resolved=False)
    run = RunRow(id=action.run_id, kind="foreground", status=RunStatus.RUNNING.value)
    session = _Session(action, run, [_Result(one=attention)])

    await AuthorityStore(session).complete(
        action.id, status=ActionStatus.UNCERTAIN, result={"message": "ambiguous"}, external_id="provider-123"
    )

    assert attention.state == "uncertain"
    assert attention.resolved is False
    assert attention.detail["external_id"] == "provider-123"
    assert run.status == RunStatus.UNCERTAIN.value
    assert run.finished_at is None


@pytest.mark.asyncio
async def test_reconciliation_cas_loss_does_not_raise_attention() -> None:
    action = _executing_action()
    action.execution_started_at = datetime.now(UTC) - timedelta(minutes=10)
    run = RunRow(id=action.run_id, kind="foreground", status=RunStatus.RUNNING.value)
    session = _Session(action, run, [_Result(many=[action]), _Result(rowcount=0)])

    count = await AuthorityStore(session).reconcile_stale_executions(stale_before=datetime.now(UTC) - timedelta(minutes=5))

    assert count == 0
    assert action.status == ActionStatus.EXECUTING.value
    assert session.added == []


@pytest.mark.asyncio
async def test_finish_run_cannot_overwrite_uncertain_run() -> None:
    run = RunRow(id=uuid4(), kind="foreground", status=RunStatus.UNCERTAIN.value)
    session = _Session(run=run)
    await AuthorityStore(session).finish_run(run.id, succeeded=True)
    assert run.status == RunStatus.UNCERTAIN.value
    assert run.finished_at is None


def test_schedule_once_preserves_explicit_offset() -> None:
    due = next_run("once", "2026-09-07T07:00:00+02:00", "UTC")
    assert due == datetime(2026, 9, 7, 5, 0, tzinfo=UTC)


def test_schedule_interval_has_one_minute_floor() -> None:
    now = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
    assert next_run("interval", "0", "Africa/Johannesburg", now=now) == now + timedelta(minutes=1)


def test_schedule_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError, match="schedule_kind"):
        next_run("moonphase", "full", "Africa/Johannesburg")


def test_once_schedule_is_disabled_before_execution() -> None:
    from atlas.persistence.models import ScheduledTaskRow
    row = ScheduledTaskRow(
        title="Once", prompt="Do it", schedule_kind="once", schedule_value="2026-09-07T07:00:00",
        timezone="Africa/Johannesburg", enabled=True, next_run_at=datetime(2026, 9, 7, 5, 0, tzinfo=UTC),
    )
    now = datetime(2026, 9, 7, 5, 0, tzinfo=UTC)
    ScheduleService(object(), "Africa/Johannesburg").advance_before_run(row, now=now)
    assert row.enabled is False
    assert row.last_status == "running"
    assert row.last_run_at == now


def test_recurring_schedule_advances_before_execution() -> None:
    from atlas.persistence.models import ScheduledTaskRow
    row = ScheduledTaskRow(
        title="Repeat", prompt="Do it", schedule_kind="interval", schedule_value="30",
        timezone="Africa/Johannesburg", enabled=True, next_run_at=datetime(2026, 9, 7, 5, 0, tzinfo=UTC),
    )
    now = datetime(2026, 9, 7, 5, 0, tzinfo=UTC)
    ScheduleService(object(), "Africa/Johannesburg").advance_before_run(row, now=now)
    assert row.enabled is True
    assert row.next_run_at == now + timedelta(minutes=30)


def _turn(actor: Actor, block) -> Turn:
    return Turn(transcript_id=uuid4(), actor=actor, blocks=[block])


def test_missing_rollover_marker_fails_open_to_canonical_history() -> None:
    turns = [_turn(Actor.OWNER, TextBlock(text="one")), _turn(Actor.ATLAS, TextBlock(text="two"))]
    assert context_turns(turns, uuid4()) == turns


def test_system_turns_do_not_leak_into_provider_chat_history() -> None:
    turns = [
        _turn(Actor.SYSTEM, TextBlock(text="internal marker")),
        _turn(Actor.OWNER, TextBlock(text="hello")),
    ]
    assert turns_to_provider_messages(turns) == [{"role": "user", "content": "hello"}]


def test_tool_evidence_is_bounded_before_projection() -> None:
    huge = "x" * 7000
    turn = _turn(Actor.TOOL, ToolObservationBlock(operation="demo.read", phase="succeeded", detail={"data": huge}))
    message = turns_to_provider_messages([turn])[0]["content"]
    assert len(message) < 6100
    assert message.endswith("…")


def _git_project(tmp_path: Path, name: str = "Demo") -> tuple[Path, ProjectFolderService]:
    import subprocess
    project = tmp_path / name
    project.mkdir()
    (project / "app.py").write_text("value = 1\n")
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    subprocess.run(["git", "-C", str(project), "config", "user.email", "atlas-test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(project), "config", "user.name", "Atlas Test"], check=True)
    subprocess.run(["git", "-C", str(project), "add", "app.py"], check=True)
    subprocess.run(["git", "-C", str(project), "commit", "-qm", "baseline"], check=True)
    return project, ProjectFolderService(tmp_path, "/home/jaco/Projects", tmp_path / ".checkpoints")


def test_project_apply_rejects_content_not_bound_to_preview(tmp_path: Path) -> None:
    _, service = _git_project(tmp_path)
    preview = service.preview_file("Demo/app.py", "value = 2\n")
    with pytest.raises(ValueError, match="does not match its preview"):
        service.apply_file("Demo/app.py", "value = 3\n", preview["expected_sha256"], preview["change_token"])


def test_project_create_refuses_file_that_appeared_after_preview(tmp_path: Path) -> None:
    project, service = _git_project(tmp_path)
    preview = service.preview_file("Demo/new.py", "created = True\n")
    (project / "new.py").write_text("owner = True\n")
    with pytest.raises(ValueError, match="appeared after Atlas previewed"):
        service.apply_file("Demo/new.py", "created = True\n", preview["expected_sha256"], preview["change_token"])


def test_project_move_cannot_cross_project_boundary(tmp_path: Path) -> None:
    project, service = _git_project(tmp_path, "One")
    (tmp_path / "Two").mkdir()
    (tmp_path / "Two" / "README.md").write_text("two\n")
    expected = service._sha256(project / "app.py")
    with pytest.raises(ValueError, match="same project"):
        service.move_file("One/app.py", "Two/app.py", expected)


def test_project_delete_refuses_stale_hash(tmp_path: Path) -> None:
    project, service = _git_project(tmp_path)
    expected = service._sha256(project / "app.py")
    (project / "app.py").write_text("owner = 2\n")
    with pytest.raises(ValueError, match="changed after Atlas read"):
        service.delete_file("Demo/app.py", expected)
    assert (project / "app.py").exists()


@pytest.mark.parametrize("name", [".env.local", "id_rsa", "deploy.key", "api-token.txt", "credentials.json"])
def test_project_protected_write_names_fail_closed(tmp_path: Path, name: str) -> None:
    project = tmp_path / "Demo"
    project.mkdir()
    service = ProjectFolderService(tmp_path, "/home/jaco/Projects", tmp_path / ".checkpoints")
    with pytest.raises(ValueError, match="protected"):
        service.preview_file(f"Demo/{name}", "secret\n")
