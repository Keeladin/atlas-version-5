from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from atlas.persistence.models import TranscriptIndexChunkRow, TranscriptIndexStateRow

from .indexer import INDEX_VERSION

_RRF_K = 60


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
        query_embedding: list[float] | None = None,
        embedding_model: str | None = None,
    ) -> list[dict[str, object]]:
        normalized = query.strip()
        if not normalized:
            return []
        bounded_limit = max(1, min(limit, 10))
        candidate_limit = max(20, min(40, bounded_limit * 4))

        tsquery = func.websearch_to_tsquery("simple", normalized)
        lexical_rank = func.ts_rank_cd(
            TranscriptIndexChunkRow.search_vector, tsquery
        ).label("lexical_rank")
        lexical = select(TranscriptIndexChunkRow, lexical_rank).where(
            TranscriptIndexChunkRow.index_version == INDEX_VERSION,
            TranscriptIndexChunkRow.search_vector.op("@@")(tsquery),
        )
        lexical = self._constraints(
            lexical,
            transcript_id=transcript_id,
            before_sequence=before_sequence,
            exclude_chunk_ids=exclude_chunk_ids,
        )
        lexical_rows = (
            await self.session.execute(
                lexical.order_by(
                    lexical_rank.desc(), TranscriptIndexChunkRow.end_sequence.desc()
                ).limit(candidate_limit)
            )
        ).all()

        semantic_rows = []
        if query_embedding is not None and embedding_model:
            distance = TranscriptIndexChunkRow.embedding.cosine_distance(query_embedding).label(
                "semantic_distance"
            )
            semantic = select(TranscriptIndexChunkRow, distance).where(
                TranscriptIndexChunkRow.index_version == INDEX_VERSION,
                TranscriptIndexChunkRow.embedding.is_not(None),
                TranscriptIndexChunkRow.embedding_model == embedding_model,
            )
            semantic = self._constraints(
                semantic,
                transcript_id=transcript_id,
                before_sequence=before_sequence,
                exclude_chunk_ids=exclude_chunk_ids,
            )
            semantic_rows = (
                await self.session.execute(
                    semantic.order_by(distance.asc(), TranscriptIndexChunkRow.end_sequence.desc()).limit(
                        candidate_limit
                    )
                )
            ).all()

        combined: dict[str, dict[str, object]] = {}
        scores: dict[str, float] = {}
        for position, (chunk, score) in enumerate(lexical_rows, start=1):
            key = str(chunk.id)
            item = combined.setdefault(key, self._project(chunk))
            item["lexical_rank"] = float(score or 0.0)
            sources = item["retrieval_sources"]
            assert isinstance(sources, list)
            sources.append("lexical")
            scores[key] = scores.get(key, 0.0) + 1.1 / (_RRF_K + position)

        for position, (chunk, distance_value) in enumerate(semantic_rows, start=1):
            key = str(chunk.id)
            item = combined.setdefault(key, self._project(chunk))
            distance_float = float(distance_value if distance_value is not None else 2.0)
            item["semantic_similarity"] = 1.0 - distance_float
            sources = item["retrieval_sources"]
            assert isinstance(sources, list)
            sources.append("semantic")
            scores[key] = scores.get(key, 0.0) + 1.0 / (_RRF_K + position)

        for key, item in combined.items():
            item["hybrid_rank"] = scores.get(key, 0.0)

        ordered = sorted(
            combined.values(),
            key=lambda item: (
                float(item.get("hybrid_rank") or 0.0),
                int(item.get("end_sequence") or 0),
            ),
            reverse=True,
        )
        return ordered[:bounded_limit]

    @staticmethod
    def _constraints(statement, *, transcript_id, before_sequence, exclude_chunk_ids):
        if transcript_id is not None:
            statement = statement.where(TranscriptIndexChunkRow.transcript_id == transcript_id)
        if before_sequence is not None:
            statement = statement.where(TranscriptIndexChunkRow.end_sequence < before_sequence)
        if exclude_chunk_ids:
            statement = statement.where(TranscriptIndexChunkRow.id.not_in(exclude_chunk_ids))
        return statement

    async def coverage(
        self,
        transcript_id: UUID | None = None,
        *,
        embedding_model: str | None = None,
        embedding_dimensions: int | None = None,
    ) -> dict[str, object]:
        chunk_query = select(
            func.count(TranscriptIndexChunkRow.id),
            func.count(func.distinct(TranscriptIndexChunkRow.transcript_id)),
        ).where(TranscriptIndexChunkRow.index_version == INDEX_VERSION)
        state_query = select(func.count()).select_from(TranscriptIndexStateRow).where(
            TranscriptIndexStateRow.index_version == INDEX_VERSION
        )
        embedded_query = select(func.count()).select_from(TranscriptIndexChunkRow).where(
            TranscriptIndexChunkRow.index_version == INDEX_VERSION,
            TranscriptIndexChunkRow.embedding.is_not(None),
        )
        if embedding_model:
            embedded_query = embedded_query.where(
                TranscriptIndexChunkRow.embedding_model == embedding_model
            )
        if embedding_dimensions is not None:
            embedded_query = embedded_query.where(
                TranscriptIndexChunkRow.embedding_dimensions == embedding_dimensions
            )
        if transcript_id is not None:
            chunk_query = chunk_query.where(TranscriptIndexChunkRow.transcript_id == transcript_id)
            state_query = state_query.where(TranscriptIndexStateRow.transcript_id == transcript_id)
            embedded_query = embedded_query.where(TranscriptIndexChunkRow.transcript_id == transcript_id)
        chunk_count, transcript_count = (await self.session.execute(chunk_query)).one()
        state_count = (await self.session.execute(state_query)).scalar_one()
        embedded_count = (await self.session.execute(embedded_query)).scalar_one()
        return {
            "index_version": INDEX_VERSION,
            "chunks": int(chunk_count or 0),
            "embedded_chunks": int(embedded_count or 0),
            "embedding_model": embedding_model,
            "embedding_dimensions": embedding_dimensions,
            "transcripts_with_chunks": int(transcript_count or 0),
            "transcripts_processed": int(state_count or 0),
        }

    @staticmethod
    def _project(chunk: TranscriptIndexChunkRow) -> dict[str, object]:
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
            "lexical_rank": None,
            "semantic_similarity": None,
            "hybrid_rank": 0.0,
            "retrieval_sources": [],
            "index_version": chunk.index_version,
        }
