"""Durable, replayable owner-facing events for inference runs."""
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from atlas.persistence.models import RunEventRow


async def append_run_event(
    session: AsyncSession,
    run_id: UUID,
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> RunEventRow:
    row = RunEventRow(run_id=run_id, event_type=event_type, payload=payload or {})
    session.add(row)
    await session.flush()
    return row


async def run_events_after(
    session: AsyncSession,
    run_id: UUID,
    after_id: int,
    *,
    limit: int = 250,
) -> list[RunEventRow]:
    rows = (await session.execute(
        select(RunEventRow)
        .where(RunEventRow.run_id == run_id, RunEventRow.id > after_id)
        .order_by(RunEventRow.id.asc())
        .limit(max(1, min(limit, 1000)))
    )).scalars().all()
    return list(rows)
