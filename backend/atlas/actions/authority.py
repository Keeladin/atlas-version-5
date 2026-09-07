import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from atlas.persistence.models import (
    ActionRow,
    OwnerAttentionRow,
    RunRow,
    ScheduledTaskRow,
    TranscriptRow,
)

from .models import ActionStatus, RunKind, RunStatus

OWNER_PRINCIPAL = "owner:jaco"
DEFAULT_PROPOSAL_TTL = timedelta(hours=1)


class ProposalIntegrityError(ValueError):
    pass


class ForegroundBusy(ValueError):
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




def _owner_action_context(action: ActionRow) -> dict[str, Any]:
    evidence = action.evidence or {}
    proposal = evidence.get("proposal")
    arguments = proposal.get("arguments") if isinstance(proposal, dict) else evidence.get("arguments")
    if not isinstance(arguments, dict):
        arguments = {}

    operation = action.operation
    if operation == "storage.projects.apply":
        return {"display_label": "Update project file", "target": str(arguments.get("path") or "") or None}
    if operation == "storage.projects.delete":
        return {"display_label": "Delete project file", "target": str(arguments.get("path") or "") or None}
    if operation == "storage.projects.move":
        source = str(arguments.get("source_path") or "")
        target = str(arguments.get("target_path") or "")
        return {"display_label": "Move project file", "target": f"{source} → {target}" if source or target else None}
    if operation == "gmail.message.send":
        recipient = str(arguments.get("to") or "")
        subject = str(arguments.get("subject") or "")
        target = " · ".join(part for part in (recipient, subject) if part)
        return {"display_label": "Send email", "target": target or None}
    if operation.startswith("calendar."):
        title = str(arguments.get("summary") or arguments.get("title") or "")
        return {"display_label": "Update calendar", "target": title or None}
    return {"display_label": (evidence.get("summary") or operation), "target": None}


class AuthorityStore:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_run(self, *, transcript_id: UUID | None, intent: str, kind: RunKind = RunKind.FOREGROUND) -> UUID:
        if kind == RunKind.FOREGROUND and transcript_id is not None:
            await self.session.execute(select(TranscriptRow.id).where(TranscriptRow.id == transcript_id).with_for_update())
            active = (await self.session.execute(select(RunRow.id).where(
                RunRow.transcript_id == transcript_id, RunRow.kind == kind.value, RunRow.inference_active.is_(True)
            ))).scalar_one_or_none()
            if active is not None:
                raise ForegroundBusy("Atlas is still handling the previous message in this conversation")
        run = RunRow(kind=kind.value, status=RunStatus.RUNNING.value, transcript_id=transcript_id, intent=intent,
            inference_active=True, inference_status="running", heartbeat_at=datetime.now(UTC))
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
        await self._lock_run(run_id)
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
        await self._recompute_run(run_id)
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
        await self._lock_run(action.run_id)
        action = await self.session.get(ActionRow, action.id, populate_existing=True)
        proposal = self.verify_proposal(action)
        result = await self.session.execute(
            update(ActionRow)
            .where(ActionRow.id == action.id, ActionRow.status == ActionStatus.PREPARED.value)
            .values(status=ActionStatus.EXECUTING.value, execution_started_at=datetime.now(UTC), updated_at=datetime.now(UTC))
        )
        if result.rowcount != 1:
            raise ProposalIntegrityError("Action proposal is no longer executable")
        await self.session.flush()
        await self._recompute_run(action.run_id)
        return proposal

    async def begin_automatic_execution(
        self, *, run_id: UUID, operation: str, arguments: dict[str, Any], summary: str, capability_id: str
    ) -> UUID:
        await self._lock_run(run_id)
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
        items: list[dict[str, Any]] = []
        for row in rows:
            detail = dict(row.detail or {})
            if row.action_id is not None:
                action = await self.session.get(ActionRow, row.action_id)
                if action is not None:
                    detail.update(_owner_action_context(action))
            items.append({
                "id": str(row.id), "action_id": str(row.action_id) if row.action_id else None, "state": row.state,
                "title": row.title, "detail": detail, "created_at": row.created_at,
            })
        return items

    async def dismiss_interruption(self, attention_id: UUID) -> None:
        attention = (await self.session.execute(
            select(OwnerAttentionRow).where(OwnerAttentionRow.id == attention_id).with_for_update()
        )).scalar_one_or_none()
        if attention is None:
            raise LookupError("Attention item not found")
        if attention.state != "interrupted" or attention.action_id is not None:
            raise ProposalIntegrityError("Only interruption notices can be dismissed")
        if attention.resolved:
            return
        attention.resolved = True
        attention.resolved_at = datetime.now(UTC)

    async def acknowledge_uncertain(self, action_id: UUID) -> None:
        action = await self.session.get(ActionRow, action_id)
        if action is None:
            raise LookupError("Action not found")
        await self._lock_run(action.run_id)
        action = await self.session.get(ActionRow, action.id, populate_existing=True)
        if action.status != ActionStatus.UNCERTAIN.value:
            raise ProposalIntegrityError("Only uncertain actions can be acknowledged")
        attention = (await self.session.execute(
            select(OwnerAttentionRow).where(OwnerAttentionRow.action_id == action.id, OwnerAttentionRow.resolved.is_(False))
        )).scalar_one_or_none()
        if attention is None:
            raise LookupError("Unresolved attention item not found")
        now = datetime.now(UTC)
        attention.resolved = True
        attention.resolved_at = now
        action.evidence = {**(action.evidence or {}), "owner_acknowledged_at": now.isoformat()}

    async def recent_activity(self, limit: int = 8) -> list[dict[str, Any]]:
        rows = (await self.session.execute(
            select(ActionRow).order_by(ActionRow.created_at.desc()).limit(max(1, min(limit, 20)))
        )).scalars().all()
        return [{"id": str(row.id), "operation": row.operation, "status": row.status,
                 "summary": (row.evidence or {}).get("summary") or row.operation, "created_at": row.created_at} for row in rows]

    async def action_for_decision(self, action_id: UUID) -> ActionRow | None:
        return await self.session.get(ActionRow, action_id)

    async def _lock_run(self, run_id: UUID) -> RunRow | None:
        return await self.session.get(RunRow, run_id, with_for_update=True, populate_existing=True)

    async def _recompute_run(self, run_id: UUID) -> None:
        run = await self._lock_run(run_id)
        if run is None:
            return
        await self.session.flush()
        statuses = set((await self.session.execute(
            select(ActionRow.status).where(ActionRow.run_id == run_id)
        )).scalars().all())
        if "uncertain" in statuses:
            status = "uncertain"
        elif "executing" in statuses:
            status = "running"
        elif "prepared" in statuses:
            status = "waiting_for_owner"
        elif run.inference_active:
            status = "running"
        elif "failed" in statuses or run.inference_status in {"failed", "interrupted"}:
            status = "failed" if run.inference_status != "interrupted" else "interrupted"
        elif "cancelled" in statuses:
            status = "cancelled"
        else:
            status = "succeeded"
        run.status = status
        run.finished_at = datetime.now(UTC) if status in {"succeeded", "failed", "cancelled", "interrupted"} else None
        if run.schedule_id is not None:
            await self.session.execute(update(ScheduledTaskRow).where(ScheduledTaskRow.id == run.schedule_id).values(last_status=status))

    async def finish_run(self, run_id: UUID, *, succeeded: bool = True) -> None:
        run = await self._lock_run(run_id)
        if run is not None:
            if not run.inference_active or run.inference_status != "running":
                raise ProposalIntegrityError("Inference no longer owns this run")
            run.inference_active = False
            run.inference_status = "succeeded" if succeeded else "failed"
            await self.session.flush()
            await self._recompute_run(run_id)

    async def cancel(self, action: ActionRow) -> None:
        await self._lock_run(action.run_id)
        action = await self.session.get(ActionRow, action.id, populate_existing=True)
        result = await self.session.execute(
            update(ActionRow).where(ActionRow.id == action.id, ActionRow.status == ActionStatus.PREPARED.value)
            .values(status=ActionStatus.CANCELLED.value, updated_at=datetime.now(UTC),
                evidence={**(action.evidence or {}), "decision": "cancelled"})
        )
        if result.rowcount != 1:
            raise ProposalIntegrityError("Only a prepared action can be cancelled")
        await self._resolve_attention_and_run(action)

    async def complete(
        self, action_id: UUID, *, status: ActionStatus, result: dict[str, Any] | None = None,
        external_id: str | None = None, resolve_run: bool = True,
    ) -> None:
        if status not in {ActionStatus.SUCCEEDED, ActionStatus.FAILED, ActionStatus.UNCERTAIN}:
            raise ValueError("Execution must end as succeeded, failed, or uncertain")
        action = await self.session.get(ActionRow, action_id)
        if action is None:
            raise ProposalIntegrityError("Executing action state was lost")
        await self._lock_run(action.run_id)
        action = await self.session.get(ActionRow, action_id, populate_existing=True)
        decision = "automatic" if (action.evidence or {}).get("automatic") else "approved"
        evidence = {**(action.evidence or {}), "decision": decision, "result": result or {}}
        if external_id:
            evidence["external_id"] = external_id
        changed = await self.session.execute(
            update(ActionRow).where(ActionRow.id == action_id, ActionRow.status == ActionStatus.EXECUTING.value)
            .values(status=status.value, updated_at=datetime.now(UTC), evidence=evidence)
        )
        if changed.rowcount != 1:
            raise ProposalIntegrityError("Executing action state was lost")
        if status == ActionStatus.UNCERTAIN:
            await self._mark_uncertain(action)
        else:
            await self._resolve_attention_and_run(action)


    async def reconcile_stale_executions(self, *, stale_before: datetime, observation_sink=None) -> int:
        rows = (await self.session.execute(
            select(ActionRow).where(
                ActionRow.status == ActionStatus.EXECUTING.value,
                ActionRow.execution_started_at.is_not(None),
                ActionRow.execution_started_at < stale_before,
            )
        )).scalars().all()
        reconciled = 0
        for action in rows:
            run = await self._lock_run(action.run_id)
            if run is not None and run.inference_active and run.heartbeat_at and run.heartbeat_at >= stale_before:
                continue
            result = await self.session.execute(
                update(ActionRow)
                .where(ActionRow.id == action.id, ActionRow.status == ActionStatus.EXECUTING.value)
                .values(status=ActionStatus.UNCERTAIN.value, updated_at=datetime.now(UTC))
            )
            if result.rowcount != 1:
                continue
            action.status = ActionStatus.UNCERTAIN.value
            await self._mark_uncertain(action, reason="Execution was abandoned before Atlas could confirm the external outcome.")
            if observation_sink is not None:
                await observation_sink(action, run)
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
            if run.schedule_id is not None:
                await self.session.execute(update(ScheduledTaskRow).where(ScheduledTaskRow.id == run.schedule_id).values(last_status="uncertain"))

    async def _resolve_attention_and_run(self, action: ActionRow) -> None:
        attention = (await self.session.execute(
            select(OwnerAttentionRow).where(OwnerAttentionRow.action_id == action.id, OwnerAttentionRow.resolved.is_(False))
        )).scalar_one_or_none()
        if attention is not None:
            attention.resolved = True
            attention.resolved_at = datetime.now(UTC)
        await self._recompute_run(action.run_id)
