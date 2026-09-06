import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from atlas.persistence.models import ActionRow, OwnerAttentionRow, RunRow

from .models import ActionStatus, RunKind, RunStatus

OWNER_PRINCIPAL = "owner:jaco"
DEFAULT_PROPOSAL_TTL = timedelta(hours=1)


class ProposalIntegrityError(ValueError):
    pass


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _target_hash(*, operation: str, arguments: dict[str, Any], principal: str, created_at: str, expires_at: str, capability_id: str) -> str:
    payload = {
        "operation": operation,
        "arguments": arguments,
        "principal": principal,
        "created_at": created_at,
        "expires_at": expires_at,
        "capability_id": capability_id,
    }
    return hashlib.sha256(_canonical_json(payload).encode()).hexdigest()


class AuthorityStore:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_run(self, *, transcript_id: UUID | None, intent: str, kind: RunKind = RunKind.FOREGROUND) -> UUID:
        run = RunRow(kind=kind.value, status=RunStatus.RUNNING.value, transcript_id=transcript_id, intent=intent)
        self.session.add(run)
        await self.session.flush()
        return run.id

    async def record_execution(self, *, run_id: UUID, operation: str, arguments: dict[str, Any], status: str, summary: str, output: Any | None = None) -> UUID:
        action_status = ActionStatus.SUCCEEDED.value if status == "succeeded" else ActionStatus.FAILED.value
        now = datetime.now(UTC)
        created_at = now.isoformat()
        target_hash = _target_hash(
            operation=operation, arguments=arguments, principal=OWNER_PRINCIPAL,
            created_at=created_at, expires_at=created_at, capability_id=operation.split(".", 1)[0],
        )
        action = ActionRow(
            run_id=run_id, operation=operation, target_hash=target_hash,
            status=action_status,
            evidence={"summary": summary, "result_status": status, "arguments": arguments, "output": output},
        )
        self.session.add(action)
        await self.session.flush()
        action.idempotency_key = f"atlas:{action.id}"
        await self.session.flush()
        return action.id

    async def prepare_proposal(self, *, run_id: UUID, operation: str, arguments: dict[str, Any], title: str, capability_id: str = "") -> UUID:
        run = await self.session.get(RunRow, run_id)
        if run is not None:
            run.status = RunStatus.WAITING_FOR_OWNER.value
        now = datetime.now(UTC)
        expires = now + DEFAULT_PROPOSAL_TTL
        proposal = {
            "operation": operation,
            "arguments": arguments,
            "principal": OWNER_PRINCIPAL,
            "created_at": now.isoformat(),
            "expires_at": expires.isoformat(),
            "capability_id": capability_id or operation.split(".", 1)[0],
            "title": title,
        }
        target_hash = _target_hash(
            operation=proposal["operation"], arguments=proposal["arguments"], principal=proposal["principal"],
            created_at=proposal["created_at"], expires_at=proposal["expires_at"], capability_id=proposal["capability_id"],
        )
        action = ActionRow(
            run_id=run_id, operation=operation, target_hash=target_hash,
            status=ActionStatus.PREPARED.value,
            evidence={"proposal": proposal, "summary": title},
        )
        self.session.add(action)
        await self.session.flush()
        action.idempotency_key = f"atlas:{action.id}"
        self.session.add(OwnerAttentionRow(
            run_id=run_id, action_id=action.id, state="approval_required", title=title,
            detail={"operation": operation, "arguments": arguments, "expires_at": proposal["expires_at"]},
        ))
        await self.session.flush()
        return action.id

    def verify_proposal(self, action: ActionRow) -> dict[str, Any]:
        proposal = (action.evidence or {}).get("proposal")
        if not isinstance(proposal, dict):
            raise ProposalIntegrityError("Action proposal payload is missing")
        required = ("operation", "arguments", "principal", "created_at", "expires_at", "capability_id")
        if any(key not in proposal for key in required) or not isinstance(proposal.get("arguments"), dict):
            raise ProposalIntegrityError("Action proposal payload is incomplete")
        if proposal["operation"] != action.operation or proposal["principal"] != OWNER_PRINCIPAL:
            raise ProposalIntegrityError("Action proposal identity no longer matches")
        try:
            expires_at = datetime.fromisoformat(str(proposal["expires_at"]))
        except ValueError as exc:
            raise ProposalIntegrityError("Action proposal expiry is invalid") from exc
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if datetime.now(UTC) >= expires_at:
            raise ProposalIntegrityError("Action proposal has expired")
        expected = _target_hash(
            operation=str(proposal["operation"]), arguments=proposal["arguments"], principal=str(proposal["principal"]),
            created_at=str(proposal["created_at"]), expires_at=str(proposal["expires_at"]), capability_id=str(proposal["capability_id"]),
        )
        if not hmac.compare_digest(expected, action.target_hash):
            raise ProposalIntegrityError("Action proposal integrity check failed")
        return proposal

    async def begin_execution(self, action: ActionRow) -> dict[str, Any]:
        proposal = self.verify_proposal(action)
        result = await self.session.execute(
            update(ActionRow)
            .where(ActionRow.id == action.id, ActionRow.status == ActionStatus.PREPARED.value)
            .values(status=ActionStatus.EXECUTING.value, execution_started_at=datetime.now(UTC), updated_at=datetime.now(UTC))
        )
        if result.rowcount != 1:
            raise ProposalIntegrityError("Action proposal is no longer executable")
        await self.session.flush()
        return proposal

    async def begin_automatic_execution(
        self, *, run_id: UUID, operation: str, arguments: dict[str, Any], summary: str, capability_id: str
    ) -> UUID:
        now = datetime.now(UTC)
        created_at = now.isoformat()
        target_hash = _target_hash(
            operation=operation, arguments=arguments, principal=OWNER_PRINCIPAL,
            created_at=created_at, expires_at=created_at, capability_id=capability_id,
        )
        action = ActionRow(
            run_id=run_id, operation=operation, target_hash=target_hash,
            status=ActionStatus.EXECUTING.value, execution_started_at=now,
            evidence={
                "summary": summary, "arguments": arguments, "automatic": True,
                "started_at": created_at, "capability_id": capability_id,
            },
        )
        self.session.add(action)
        await self.session.flush()
        action.idempotency_key = f"atlas:{action.id}"
        await self.session.flush()
        return action.id

    async def pending(self) -> list[dict[str, Any]]:
        rows = (await self.session.execute(
            select(OwnerAttentionRow).where(OwnerAttentionRow.resolved.is_(False)).order_by(OwnerAttentionRow.created_at)
        )).scalars().all()
        return [{"id": str(row.id), "action_id": str(row.action_id) if row.action_id else None, "state": row.state,
                 "title": row.title, "detail": row.detail, "created_at": row.created_at} for row in rows]

    async def recent_activity(self, limit: int = 8) -> list[dict[str, Any]]:
        rows = (await self.session.execute(
            select(ActionRow).order_by(ActionRow.created_at.desc()).limit(max(1, min(limit, 20)))
        )).scalars().all()
        return [{"id": str(row.id), "operation": row.operation, "status": row.status,
                 "summary": (row.evidence or {}).get("summary") or row.operation, "created_at": row.created_at} for row in rows]

    async def action_for_decision(self, action_id: UUID) -> ActionRow | None:
        return await self.session.get(ActionRow, action_id)

    async def finish_run(self, run_id: UUID, *, succeeded: bool = True) -> None:
        run = await self.session.get(RunRow, run_id)
        if run is not None and run.status == RunStatus.RUNNING.value:
            run.status = RunStatus.SUCCEEDED.value if succeeded else RunStatus.FAILED.value
            run.finished_at = datetime.now(UTC)

    async def cancel(self, action: ActionRow) -> None:
        action.status = ActionStatus.CANCELLED.value
        action.updated_at = datetime.now(UTC)
        action.evidence = {**(action.evidence or {}), "decision": "cancelled"}
        await self._resolve_attention_and_run(action, RunStatus.CANCELLED.value)

    async def complete(
        self, action_id: UUID, *, status: ActionStatus, result: dict[str, Any] | None = None,
        external_id: str | None = None, resolve_run: bool = True,
    ) -> None:
        if status not in {ActionStatus.SUCCEEDED, ActionStatus.FAILED, ActionStatus.UNCERTAIN}:
            raise ValueError("Execution must end as succeeded, failed, or uncertain")
        action = await self.session.get(ActionRow, action_id)
        if action is None or action.status != ActionStatus.EXECUTING.value:
            raise ProposalIntegrityError("Executing action state was lost")
        action.status = status.value
        action.updated_at = datetime.now(UTC)
        decision = "automatic" if (action.evidence or {}).get("automatic") else "approved"
        evidence = {**(action.evidence or {}), "decision": decision, "result": result or {}}
        if external_id:
            evidence["external_id"] = external_id
        action.evidence = evidence
        if status == ActionStatus.UNCERTAIN:
            await self._mark_uncertain(action)
        elif resolve_run:
            run_status = RunStatus.SUCCEEDED.value if status == ActionStatus.SUCCEEDED else RunStatus.FAILED.value
            await self._resolve_attention_and_run(action, run_status)


    async def reconcile_stale_executions(self, *, stale_before: datetime) -> int:
        rows = (await self.session.execute(
            select(ActionRow).where(
                ActionRow.status == ActionStatus.EXECUTING.value,
                ActionRow.execution_started_at.is_not(None),
                ActionRow.execution_started_at < stale_before,
            )
        )).scalars().all()
        reconciled = 0
        for action in rows:
            result = await self.session.execute(
                update(ActionRow)
                .where(ActionRow.id == action.id, ActionRow.status == ActionStatus.EXECUTING.value)
                .values(status=ActionStatus.UNCERTAIN.value, updated_at=datetime.now(UTC))
            )
            if result.rowcount != 1:
                continue
            action.status = ActionStatus.UNCERTAIN.value
            await self._mark_uncertain(action, reason="Execution was abandoned before Atlas could confirm the external outcome.")
            reconciled += 1
        return reconciled

    async def _mark_uncertain(self, action: ActionRow, *, reason: str | None = None) -> None:
        detail = {
            "operation": action.operation,
            "action_id": str(action.id),
            "run_id": str(action.run_id),
            "message": reason or "Atlas started this action but cannot confirm whether it completed.",
            "execution_started_at": action.execution_started_at.isoformat() if action.execution_started_at else None,
        }
        evidence = action.evidence or {}
        external_id = evidence.get("external_id")
        if external_id:
            detail["external_id"] = external_id
        attention = (await self.session.execute(
            select(OwnerAttentionRow).where(OwnerAttentionRow.action_id == action.id, OwnerAttentionRow.resolved.is_(False))
        )).scalar_one_or_none()
        if attention is None:
            self.session.add(OwnerAttentionRow(
                run_id=action.run_id, action_id=action.id, state="uncertain",
                title=f"Uncertain action: {action.operation}", detail=detail, resolved=False,
            ))
        else:
            attention.state = "uncertain"
            attention.title = f"Uncertain action: {action.operation}"
            attention.detail = detail
        run = await self.session.get(RunRow, action.run_id)
        if run is not None:
            run.status = RunStatus.UNCERTAIN.value
            run.finished_at = None

    async def _resolve_attention_and_run(self, action: ActionRow, run_status: str) -> None:
        attention = (await self.session.execute(
            select(OwnerAttentionRow).where(OwnerAttentionRow.action_id == action.id, OwnerAttentionRow.resolved.is_(False))
        )).scalar_one_or_none()
        if attention is not None:
            attention.resolved = True
            attention.resolved_at = datetime.now(UTC)
        run = await self.session.get(RunRow, action.run_id)
        if run is not None:
            run.status = run_status
            run.finished_at = datetime.now(UTC)
