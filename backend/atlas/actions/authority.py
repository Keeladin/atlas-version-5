import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from atlas.persistence.models import ActionRow, OwnerAttentionRow, RunRow

from .models import ActionStatus, RunKind, RunStatus


def _target_hash(arguments: dict[str, Any]) -> str:
    canonical = json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


class AuthorityStore:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_run(self, *, transcript_id: UUID | None, intent: str) -> UUID:
        run = RunRow(kind=RunKind.FOREGROUND.value, status=RunStatus.RUNNING.value, transcript_id=transcript_id, intent=intent)
        self.session.add(run)
        await self.session.flush()
        return run.id

    async def record_execution(
        self,
        *,
        run_id: UUID,
        operation: str,
        arguments: dict[str, Any],
        status: str,
        summary: str,
    ) -> UUID:
        action_status = ActionStatus.SUCCEEDED.value if status == "succeeded" else ActionStatus.FAILED.value
        action = ActionRow(
            run_id=run_id,
            operation=operation,
            target_hash=_target_hash(arguments),
            status=action_status,
            evidence={"summary": summary, "result_status": status},
        )
        self.session.add(action)
        await self.session.flush()
        return action.id

    async def prepare_proposal(
        self,
        *,
        run_id: UUID,
        operation: str,
        arguments: dict[str, Any],
        title: str,
    ) -> UUID:
        run = await self.session.get(RunRow, run_id)
        if run is not None:
            run.status = RunStatus.WAITING_FOR_OWNER.value
        action = ActionRow(
            run_id=run_id,
            operation=operation,
            target_hash=_target_hash(arguments),
            status=ActionStatus.PREPARED.value,
            evidence={"proposal": {"arguments": arguments, "title": title}, "summary": title},
        )
        self.session.add(action)
        await self.session.flush()
        self.session.add(OwnerAttentionRow(
            run_id=run_id,
            action_id=action.id,
            state="approval_required",
            title=title,
            detail={"operation": operation, "arguments": arguments},
        ))
        await self.session.flush()
        return action.id

    async def pending(self) -> list[dict[str, Any]]:
        rows = (await self.session.execute(
            select(OwnerAttentionRow).where(OwnerAttentionRow.resolved.is_(False)).order_by(OwnerAttentionRow.created_at)
        )).scalars().all()
        return [{
            "id": str(row.id),
            "action_id": str(row.action_id) if row.action_id else None,
            "state": row.state,
            "title": row.title,
            "detail": row.detail,
            "created_at": row.created_at,
        } for row in rows]

    async def recent_activity(self, limit: int = 8) -> list[dict[str, Any]]:
        rows = (await self.session.execute(
            select(ActionRow).order_by(ActionRow.created_at.desc()).limit(max(1, min(limit, 20)))
        )).scalars().all()
        return [{
            "id": str(row.id),
            "operation": row.operation,
            "status": row.status,
            "summary": (row.evidence or {}).get("summary") or row.operation,
            "created_at": row.created_at,
        } for row in rows]

    async def action_for_decision(self, action_id: UUID) -> ActionRow | None:
        return await self.session.get(ActionRow, action_id)

    async def finish_run(self, run_id: UUID, *, succeeded: bool = True) -> None:
        run = await self.session.get(RunRow, run_id)
        if run is not None and run.status == RunStatus.RUNNING.value:
            run.status = RunStatus.SUCCEEDED.value if succeeded else RunStatus.FAILED.value
            run.finished_at = datetime.now(UTC)

    async def resolve(self, action: ActionRow, *, approved: bool, succeeded: bool = True, evidence: dict[str, Any] | None = None) -> None:
        if not approved:
            action.status = ActionStatus.CANCELLED.value
        else:
            action.status = ActionStatus.SUCCEEDED.value if succeeded else ActionStatus.FAILED.value
        action.updated_at = datetime.now(UTC)
        if evidence is not None:
            action.evidence = evidence
        attention = (await self.session.execute(
            select(OwnerAttentionRow).where(OwnerAttentionRow.action_id == action.id, OwnerAttentionRow.resolved.is_(False))
        )).scalar_one_or_none()
        if attention is not None:
            attention.resolved = True
            attention.resolved_at = datetime.now(UTC)
        run = await self.session.get(RunRow, action.run_id)
        if run is not None:
            if not approved:
                run.status = RunStatus.CANCELLED.value
            else:
                run.status = RunStatus.SUCCEEDED.value if succeeded else RunStatus.FAILED.value
            run.finished_at = datetime.now(UTC)
