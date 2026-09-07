from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from atlas.actions.authority import (
    OWNER_PRINCIPAL,
    AuthorityStore,
    ProposalIntegrityError,
    _target_hash,
)
from atlas.persistence.models import ActionRow


def _action(*, expires_delta: timedelta = timedelta(minutes=30)) -> ActionRow:
    now = datetime.now(UTC)
    proposal = {
        "operation": "gmail.message.send",
        "arguments": {"to": "owner@example.com", "subject": "Test", "body": "Hello"},
        "principal": OWNER_PRINCIPAL,
        "created_at": now.isoformat(),
        "expires_at": (now + expires_delta).isoformat(),
        "capability_id": "google.workspace",
        "title": "Send test email",
    }
    target_hash = _target_hash(
        operation=proposal["operation"],
        arguments=proposal["arguments"],
        principal=proposal["principal"],
        created_at=proposal["created_at"],
        expires_at=proposal["expires_at"],
        capability_id=proposal["capability_id"],
    )
    return ActionRow(
        run_id=uuid4(), operation=proposal["operation"], target_hash=target_hash,
        status="prepared", evidence={"proposal": proposal},
    )


def test_exact_proposal_verifies() -> None:
    action = _action()
    store = AuthorityStore(None)  # type: ignore[arg-type]
    proposal = store.verify_proposal(action)
    assert proposal["arguments"]["subject"] == "Test"


def test_changed_payload_fails_closed() -> None:
    action = _action()
    action.evidence["proposal"]["arguments"]["subject"] = "Changed"
    store = AuthorityStore(None)  # type: ignore[arg-type]
    with pytest.raises(ProposalIntegrityError, match="integrity"):
        store.verify_proposal(action)


def test_expired_proposal_fails_closed() -> None:
    action = _action(expires_delta=timedelta(seconds=-1))
    store = AuthorityStore(None)  # type: ignore[arg-type]
    with pytest.raises(ProposalIntegrityError, match="expired"):
        store.verify_proposal(action)

class _Result:
    def __init__(self, *, one=None, many=None, rowcount=None):
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
    def __init__(self, action, run, results):
        self.action = action
        self.run = run
        self.results = list(results)
        self.added = []

    async def get(self, model, key, **kwargs):
        from atlas.persistence.models import ActionRow, RunRow
        if model is ActionRow:
            return self.action
        if model is RunRow:
            return self.run
        return None

    async def execute(self, statement):
        from sqlalchemy.sql.dml import Update
        if isinstance(statement, Update):
            if self.results and self.results[0].rowcount is not None:
                result = self.results.pop(0)
            else:
                result = _Result(rowcount=1)
            if result.rowcount == 1:
                values = statement.compile().params
                for key in ("status", "evidence", "updated_at"):
                    if key in values:
                        setattr(self.action, key, values[key])
            return result
        descriptions = getattr(statement, 'column_descriptions', [])
        if descriptions and descriptions[0].get('name') == 'status':
            statuses = [self.action.status] if self.action is not None else []
            if self.run is not None and self.run.status == 'uncertain':
                statuses.append('uncertain')
            return _Result(many=statuses)
        return self.results.pop(0)

    async def flush(self):
        pass

    def add(self, value):
        self.added.append(value)


@pytest.mark.asyncio
async def test_uncertain_completion_stays_visible_and_marks_run_uncertain() -> None:
    from atlas.actions.models import ActionStatus, RunStatus
    from atlas.persistence.models import OwnerAttentionRow, RunRow

    action = _action()
    action.status = ActionStatus.EXECUTING.value
    action.execution_started_at = datetime.now(UTC) - timedelta(minutes=1)
    run = RunRow(id=action.run_id, kind="foreground", status="running")
    session = _Session(action, run, [_Result(one=None)])

    await AuthorityStore(session).complete(
        action.id, status=ActionStatus.UNCERTAIN, result={"message": "ambiguous dispatch"}
    )

    assert action.status == ActionStatus.UNCERTAIN.value
    assert run.status == RunStatus.UNCERTAIN.value
    assert run.finished_at is None
    attention = next(item for item in session.added if isinstance(item, OwnerAttentionRow))
    assert attention.state == "uncertain"
    assert attention.resolved is False

@pytest.mark.asyncio
async def test_stale_executing_action_reconciles_to_uncertain() -> None:
    from atlas.actions.models import ActionStatus, RunStatus
    from atlas.persistence.models import RunRow

    action = _action()
    action.status = ActionStatus.EXECUTING.value
    action.execution_started_at = datetime.now(UTC) - timedelta(minutes=10)
    run = RunRow(id=action.run_id, kind="foreground", status="running")
    session = _Session(action, run, [_Result(many=[action]), _Result(rowcount=1), _Result(one=None)])

    count = await AuthorityStore(session).reconcile_stale_executions(
        stale_before=datetime.now(UTC) - timedelta(minutes=5)
    )

    assert count == 1
    assert action.status == ActionStatus.UNCERTAIN.value
    assert run.status == RunStatus.UNCERTAIN.value
    assert session.added[0].state == "uncertain"
