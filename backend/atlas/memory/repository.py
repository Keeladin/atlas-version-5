from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from atlas.persistence.models import TranscriptIndexChunkRow, TranscriptIndexStateRow

from .indexer import INDEX_VERSION


class MemorySearchRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def search(
        self,
        query: str,
        *,
        limit: int = 5,
        transcript_id: UUID | None = None,
        before_sequence: int | None = None,
        exclude_chunk_ids: list[UUID] | None = None,
    ) -> list[dict[str, object]]:
        normalized = query.strip()
        if not normalized:
            return []
        tsquery = func.websearch_to_tsquery("simple", normalized)
        rank = func.ts_rank_cd(TranscriptIndexChunkRow.search_vector, tsquery).label("rank")
        statement = (
            select(TranscriptIndexChunkRow, rank)
            .where(
                TranscriptIndexChunkRow.index_version == INDEX_VERSION,
                TranscriptIndexChunkRow.search_vector.op("@@")(tsquery),
            )
        )
        if transcript_id is not None:
            statement = statement.where(TranscriptIndexChunkRow.transcript_id == transcript_id)
        if before_sequence is not None:
            statement = statement.where(TranscriptIndexChunkRow.end_sequence < before_sequence)
        if exclude_chunk_ids:
            statement = statement.where(TranscriptIndexChunkRow.id.not_in(exclude_chunk_ids))
        statement = statement.order_by(rank.desc(), TranscriptIndexChunkRow.end_sequence.desc()).limit(
            max(1, min(limit, 10))
        )
        rows = (await self.session.execute(statement)).all()
        return [self._project(chunk, float(score or 0.0)) for chunk, score in rows]

    async def coverage(self, transcript_id: UUID | None = None) -> dict[str, object]:
        chunk_query = select(
            func.count(TranscriptIndexChunkRow.id),
            func.count(func.distinct(TranscriptIndexChunkRow.transcript_id)),
        ).where(TranscriptIndexChunkRow.index_version == INDEX_VERSION)
        state_query = select(func.count()).select_from(TranscriptIndexStateRow).where(
            TranscriptIndexStateRow.index_version == INDEX_VERSION
        )
        if transcript_id is not None:
            chunk_query = chunk_query.where(TranscriptIndexChunkRow.transcript_id == transcript_id)
            state_query = state_query.where(TranscriptIndexStateRow.transcript_id == transcript_id)
        chunk_count, transcript_count = (await self.session.execute(chunk_query)).one()
        state_count = (await self.session.execute(state_query)).scalar_one()
        return {
            "index_version": INDEX_VERSION,
            "chunks": int(chunk_count or 0),
            "transcripts_with_chunks": int(transcript_count or 0),
            "transcripts_processed": int(state_count or 0),
        }

    @staticmethod
    def _project(chunk: TranscriptIndexChunkRow, score: float) -> dict[str, object]:
        content = chunk.content
        if len(content) > 4_000:
            content = content[:3_997].rstrip() + "..."
        return {
            "chunk_id": str(chunk.id),
            "transcript_id": str(chunk.transcript_id),
            "start_sequence": chunk.start_sequence,
            "end_sequence": chunk.end_sequence,
            "source_turn_ids": list(chunk.source_turn_ids or []),
            "content": content,
            "lexical_rank": score,
            "index_version": chunk.index_version,
        }
