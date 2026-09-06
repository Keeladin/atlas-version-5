from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from atlas.persistence.models import ActionRow, RunRow

from .models import Action, ActionStatus, Run, RunKind, RunStatus


class ActionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_run(
        self,
        kind: RunKind,
        *,
        transcript_id: UUID | None = None,
        workspace_id: UUID | None = None,
        intent: str | None = None,
    ) -> Run:
        row = RunRow(
            kind=kind.value,
            status=RunStatus.RUNNING.value,
            transcript_id=transcript_id,
            workspace_id=workspace_id,
            intent=intent,
        )
        self.session.add(row)
        await self.session.flush()
        return Run.model_validate(row, from_attributes=True)

    async def prepare_action(
        self,
        run_id: UUID,
        *,
        operation: str,
        target_hash: str,
        idempotency_key: str | None = None,
    ) -> Action:
        row = ActionRow(
            run_id=run_id,
            operation=operation,
            target_hash=target_hash,
            status=ActionStatus.PREPARED.value,
            idempotency_key=idempotency_key,
            evidence={},
        )
        self.session.add(row)
        await self.session.flush()
        return Action.model_validate(row, from_attributes=True)

    async def set_action_status(
        self,
        row: ActionRow,
        status: ActionStatus,
        evidence: dict[str, object] | None = None,
    ) -> Action:
        row.status = status.value
        row.updated_at = datetime.now(UTC)
        if evidence is not None:
            row.evidence = evidence
        await self.session.flush()
        return Action.model_validate(row, from_attributes=True)
