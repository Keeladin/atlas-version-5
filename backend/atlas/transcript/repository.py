from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from atlas.persistence.models import TranscriptRow, TurnRow

from .models import Actor, ContentBlock, Transcript, Turn


class TranscriptRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, *, kind: str = "owner") -> Transcript:
        row = TranscriptRow(kind=kind)
        self.session.add(row)
        await self.session.flush()
        return Transcript(id=row.id, kind=row.kind, created_at=row.created_at, context_summary=row.context_summary, summarized_through_turn_id=row.summarized_through_turn_id, active_task_state=row.active_task_state or {})


    async def get_or_create_active(self) -> Transcript:
        result = await self.session.execute(
            select(TranscriptRow)
            .where(TranscriptRow.closed_at.is_(None), TranscriptRow.kind == "owner")
            .order_by(TranscriptRow.created_at.desc(), TranscriptRow.id.desc())
            .limit(1)
        )
        row = result.scalar_one_or_none()
        if row is None:
            return await self.create()
        return Transcript(id=row.id, kind=row.kind, created_at=row.created_at, closed_at=row.closed_at, context_summary=row.context_summary, summarized_through_turn_id=row.summarized_through_turn_id, active_task_state=row.active_task_state or {})


    async def get_active_task_state(self, transcript_id: UUID) -> dict:
        row = await self.session.get(TranscriptRow, transcript_id)
        if row is None:
            raise LookupError("Transcript not found")
        return dict(row.active_task_state or {})

    async def update_active_task_state(self, transcript_id: UUID, state: dict) -> None:
        row = await self.session.get(TranscriptRow, transcript_id)
        if row is None:
            raise LookupError("Transcript not found")
        row.active_task_state = state
        await self.session.flush()

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
        row = TurnRow(
            transcript_id=transcript_id,
            actor=actor.value,
            blocks=[block.model_dump(mode="json") for block in blocks],
        )
        self.session.add(row)
        await self.session.flush()
        return Turn(
            id=row.id,
            transcript_id=row.transcript_id,
            actor=Actor(row.actor),
            blocks=row.blocks,
            created_at=row.created_at,
        )

    async def list_turns(self, transcript_id: UUID) -> list[Turn]:
        result = await self.session.execute(
            select(TurnRow)
            .where(TurnRow.transcript_id == transcript_id)
            .order_by(TurnRow.created_at, TurnRow.id)
        )
        return [
            Turn(
                id=row.id,
                transcript_id=row.transcript_id,
                actor=Actor(row.actor),
                blocks=row.blocks,
                created_at=row.created_at,
            )
            for row in result.scalars()
        ]
