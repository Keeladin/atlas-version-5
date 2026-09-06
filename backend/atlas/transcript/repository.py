from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from atlas.persistence.models import TranscriptRow, TurnRow

from .models import Actor, ContentBlock, Transcript, Turn


class TranscriptRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self) -> Transcript:
        row = TranscriptRow()
        self.session.add(row)
        await self.session.flush()
        return Transcript(id=row.id, created_at=row.created_at)


    async def get_or_create_active(self) -> Transcript:
        result = await self.session.execute(
            select(TranscriptRow)
            .where(TranscriptRow.closed_at.is_(None))
            .order_by(TranscriptRow.created_at.desc(), TranscriptRow.id.desc())
            .limit(1)
        )
        row = result.scalar_one_or_none()
        if row is None:
            return await self.create()
        return Transcript(id=row.id, created_at=row.created_at, closed_at=row.closed_at)

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
