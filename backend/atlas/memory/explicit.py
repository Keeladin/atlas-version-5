"""Explicit owner memory commands (remember/correct) ride the candidate pipeline.

The owner-facing promise is an obligation row: it stays pending until the
candidate that carries the request reaches a terminal outcome, and it resolves
inside the same transaction that writes that outcome. Every terminal candidate
writer calls ``settle_explicit_obligation`` so the obligation and its command
ledger row can never disagree with the candidate state.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from atlas.persistence.models import (
    DurableMemoryRow,
    MemoryCandidateRow,
    MemoryCommandRow,
    MemoryObligationRow,
)

from .durable import APPLIED, FAILED

EXPLICIT_OBLIGATION_KINDS = frozenset({"explicit_remember", "explicit_correct"})
EXPLICIT_ORIGINS = frozenset({"explicit_owner_request", "explicit_owner_correction"})

# Owner retries re-enter the candidate from persisted evidence after the
# automatic attempt budget on the candidate itself is exhausted. This bounds
# how many such automatic exhaustions the obligation survives before it is
# terminated visibly instead of staying pending forever.
OWNER_RETRY_BUDGET = 3

_TERMINAL_RESOLUTION: dict[str, str] = {
    "reconciled": "published",
    "discarded": "discarded",
    "blocked": "blocked",
    "invalidated": "invalidated",
    "expired": "expired",
    "rejected": "rejected",
}


def obligation_kind_for(operation: str) -> str:
    return "explicit_remember" if operation == "remember" else "explicit_correct"


def origin_for(operation: str) -> str:
    return "explicit_owner_request" if operation == "remember" else "explicit_owner_correction"


async def pending_explicit_obligation(
    session: AsyncSession, candidate_id: UUID
) -> MemoryObligationRow | None:
    return (
        await session.execute(
            select(MemoryObligationRow)
            .where(
                MemoryObligationRow.subject_type == "memory_candidate",
                MemoryObligationRow.subject_id == candidate_id,
                MemoryObligationRow.kind.in_(EXPLICIT_OBLIGATION_KINDS),
                MemoryObligationRow.status == "pending",
            )
            .order_by(MemoryObligationRow.created_at.asc())
            .with_for_update()
        )
    ).scalars().first()


async def settle_explicit_obligation(
    session: AsyncSession,
    candidate: MemoryCandidateRow,
    *,
    now: datetime,
    memory_id: UUID | None = None,
    code: str | None = None,
    resolution: str | None = None,
) -> MemoryObligationRow | None:
    """Reflect ``candidate.status`` onto its pending explicit obligation.

    Runs in the caller's transaction. Non-terminal candidate states leave the
    obligation pending. ``failed`` (automatic attempt budget exhausted) keeps
    the obligation pending for an owner retry until ``OWNER_RETRY_BUDGET``
    exhaustions, after which it terminates visibly.
    """
    obligation = await pending_explicit_obligation(session, candidate.id)
    if obligation is None:
        return None
    decision = dict(candidate.decision_json or {})
    detail = str(
        code or decision.get("code") or decision.get("result") or decision.get("decision") or ""
    )
    command = (
        await session.get(MemoryCommandRow, obligation.command_id)
        if obligation.command_id is not None
        else None
    )
    status = candidate.status

    if status == "reconciled":
        if memory_id is None:
            memory_id = (
                await session.execute(
                    select(DurableMemoryRow.id).where(
                        DurableMemoryRow.originating_candidate_id == candidate.id
                    )
                )
            ).scalar_one_or_none()
        obligation.status = "resolved"
        obligation.resolution_code = "published"
        obligation.resolution_json = {
            **_carry(obligation),
            "candidate_id": str(candidate.id),
            "memory_id": str(memory_id) if memory_id is not None else None,
            "result": detail,
        }
        obligation.resolved_at = now
        if command is not None:
            command.status = APPLIED
            command.applied_at = now
            command.failed_at = None
            command.error = None
            command.replacement_memory_id = memory_id
        return obligation

    if status == "failed":
        carried = _carry(obligation)
        failure_count = int(carried.get("failure_count", 0)) + 1
        obligation.resolution_json = {
            **carried,
            "candidate_id": str(candidate.id),
            "failure_count": failure_count,
            "failure_budget": OWNER_RETRY_BUDGET,
            "last_error": detail,
        }
        if failure_count >= OWNER_RETRY_BUDGET:
            obligation.status = "resolved"
            obligation.resolution_code = "failed_retry_exhausted"
            obligation.resolved_at = now
            if command is not None:
                command.status = FAILED
                command.failed_at = now
                command.error = f"failed_retry_exhausted: {detail}"[:2_000]
        return obligation

    resolution = resolution or _TERMINAL_RESOLUTION.get(status)
    if resolution is None:
        return obligation
    obligation.status = "resolved"
    obligation.resolution_code = resolution
    obligation.resolution_json = {
        **_carry(obligation),
        "candidate_id": str(candidate.id),
        "code": detail,
    }
    obligation.resolved_at = now
    if command is not None:
        command.status = FAILED
        command.failed_at = now
        command.error = f"{resolution}: {detail}"[:2_000]
    return obligation


def _carry(obligation: MemoryObligationRow) -> dict[str, object]:
    carried = dict(obligation.resolution_json or {})
    return {
        key: carried[key]
        for key in ("failure_count", "failure_budget", "owner_retries", "last_error")
        if key in carried
    }
