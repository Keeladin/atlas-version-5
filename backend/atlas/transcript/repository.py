from collections.abc import Callable
from copy import deepcopy
from uuid import UUID, uuid4

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from atlas.persistence.models import TranscriptRow, TurnRow

from .models import Actor, ContentBlock, Transcript, Turn


class TaskStateConflict(ValueError):
    """The caller's checkpoint snapshot is no longer current."""


class TranscriptRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, *, kind: str = "owner") -> Transcript:
        row = TranscriptRow(kind=kind)
        self.session.add(row)
        await self.session.flush()
        return Transcript(id=row.id, kind=row.kind, created_at=row.created_at, context_summary=row.context_summary, summarized_through_turn_id=row.summarized_through_turn_id, active_task_state=row.active_task_state or {})


    async def get_or_create_active(self) -> Transcript:
        query = (select(TranscriptRow)
            .where(TranscriptRow.closed_at.is_(None), TranscriptRow.kind == "owner")
            .order_by(TranscriptRow.created_at.desc(), TranscriptRow.id.desc()).limit(1))
        row = (await self.session.execute(query)).scalar_one_or_none()
        if row is None:
            # First creation has no row to lock. Recheck after serializing it.
            await self.session.execute(text("SELECT pg_advisory_xact_lock(814527, 1)"))
            row = (await self.session.execute(query)).scalar_one_or_none()
            if row is None:
                return await self.create()
        return Transcript(id=row.id, kind=row.kind, created_at=row.created_at, closed_at=row.closed_at, context_summary=row.context_summary, summarized_through_turn_id=row.summarized_through_turn_id, active_task_state=row.active_task_state or {})


    async def get_active_task_state(self, transcript_id: UUID) -> dict:
        row = await self.session.get(TranscriptRow, transcript_id)
        if row is None:
            raise LookupError("Transcript not found")
        return dict(row.active_task_state or {})

    async def update_active_task_state(self, transcript_id: UUID, state: dict, *, expected_revision: int) -> dict:
        state = deepcopy(state)
        state.setdefault("task_id", str(uuid4()))
        state["revision"] = expected_revision + 1
        result = await self.session.execute(
            update(TranscriptRow)
            .where(TranscriptRow.id == transcript_id, TranscriptRow.active_task_revision == expected_revision)
            .values(active_task_state=state, active_task_revision=expected_revision + 1)
        )
        if result.rowcount != 1:
            raise TaskStateConflict("Active task changed; reload before updating")
        return state

    async def mutate_active_task_state(
        self, transcript_id: UUID, transform: Callable[[dict], dict], *, expected_task_id: str | None = None
    ) -> dict:
        # Serialize transforms over the latest state. The conditional write is
        # also available to callers holding an explicit optimistic snapshot.
        row = (await self.session.execute(
            select(TranscriptRow).where(TranscriptRow.id == transcript_id)
            .with_for_update().execution_options(populate_existing=True)
        )).scalar_one_or_none()
        if row is None:
            raise LookupError("Transcript not found")
        current = deepcopy(row.active_task_state or {})
        if expected_task_id is not None and current.get("task_id") != expected_task_id:
            raise TaskStateConflict("The response belongs to a different active task")
        updated = transform(current)
        if updated == current:
            return current
        return await self.update_active_task_state(
            transcript_id, updated, expected_revision=row.active_task_revision
        )

    async def update_context_summary(self, transcript_id: UUID, *, summary: str, summarized_through_turn_id: UUID) -> None:
        row = await self.session.get(TranscriptRow, transcript_id)
        if row is None:
            raise LookupError("Transcript not found")
        row.context_summary = summary
        row.summarized_through_turn_id = summarized_through_turn_id
        await self.session.flush()

    async def append_turn(
        self,
        transcript_id: UUID,
        actor: Actor,
        blocks: list[ContentBlock],
    ) -> Turn:
        sequence = (await self.session.execute(
            update(TranscriptRow).where(TranscriptRow.id == transcript_id)
            .values(next_turn_sequence=TranscriptRow.next_turn_sequence + 1)
            .returning(TranscriptRow.next_turn_sequence)
        )).scalar_one()
        row = TurnRow(
            transcript_id=transcript_id,
            sequence=sequence,
            actor=actor.value,
            blocks=[block.model_dump(mode="json") for block in blocks],
        )
        self.session.add(row)
        await self.session.flush()
        return Turn(
            id=row.id,
            transcript_id=row.transcript_id,
            sequence=row.sequence,
            actor=Actor(row.actor),
            blocks=row.blocks,
            created_at=row.created_at,
        )

    async def list_turns(self, transcript_id: UUID) -> list[Turn]:
        result = await self.session.execute(
            select(TurnRow)
            .where(TurnRow.transcript_id == transcript_id)
            .order_by(TurnRow.sequence)
        )
        return [
            Turn(
                id=row.id,
                transcript_id=row.transcript_id,
                sequence=row.sequence,
                actor=Actor(row.actor),
                blocks=row.blocks,
                created_at=row.created_at,
            )
            for row in result.scalars()
        ]
