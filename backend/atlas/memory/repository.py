from __future__ import annotations

from uuid import UUID

from sqlalchemy import String, cast, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from atlas.persistence.models import (
    DurableMemoryRow,
    TranscriptIndexChunkRow,
    TranscriptIndexStateRow,
    TranscriptRow,
    TurnRow,
)

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
    def _constraints(
        statement,
        *,
        transcript_id,
        before_sequence,
        exclude_chunk_ids,
    ):
        if transcript_id is not None:
            statement = statement.where(TranscriptIndexChunkRow.transcript_id == transcript_id)
        if before_sequence is not None:
            # Only whole chunks strictly before the boundary are searchable. A straddling
            # chunk is deliberately excluded and reported by coverage() so the model does
            # not treat later text inside that chunk as evidence about the earlier range.
            statement = statement.where(TranscriptIndexChunkRow.end_sequence < before_sequence)
        if exclude_chunk_ids:
            statement = statement.where(TranscriptIndexChunkRow.id.not_in(exclude_chunk_ids))

        # Owner correction/forgetting precedence is a database-side anti-join, so
        # search query size stays constant as the number of memory guards grows.
        guard = DurableMemoryRow.__table__.alias("memory_recall_guard")
        source_turn_guard = func.jsonb_build_array(cast(guard.c.source_turn_id, String))
        guarded_chunk = exists(
            select(1)
            .select_from(guard)
            .where(
                guard.c.suppresses_recall.is_(True),
                guard.c.status != "deleted",
                or_(
                    guard.c.source_turn_id.is_not(None)
                    & TranscriptIndexChunkRow.source_turn_ids.op("@>")(source_turn_guard),
                    func.strpos(
                        func.lower(TranscriptIndexChunkRow.content),
                        func.lower(guard.c.content),
                    )
                    > 0,
                ),
            )
            .correlate(TranscriptIndexChunkRow)
        )
        return statement.where(~guarded_chunk)

    async def coverage(
        self,
        transcript_id: UUID | None = None,
        *,
        before_sequence: int | None = None,
        embedding_model: str | None = None,
        embedding_dimensions: int | None = None,
    ) -> dict[str, object]:
        if before_sequence is not None and transcript_id is None:
            raise ValueError("before_sequence coverage requires transcript_id")

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
        transcript_query = select(TranscriptRow).order_by(TranscriptRow.created_at, TranscriptRow.id)
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
            transcript_query = transcript_query.where(TranscriptRow.id == transcript_id)

        chunk_count, transcript_count = (await self.session.execute(chunk_query)).one()
        state_count = (await self.session.execute(state_query)).scalar_one()
        embedded_count = (await self.session.execute(embedded_query)).scalar_one()
        transcripts = list((await self.session.execute(transcript_query)).scalars())
        transcript_ids = [transcript.id for transcript in transcripts]

        turn_stats: dict[UUID, tuple[int | None, int | None, int]] = {}
        states: dict[UUID, TranscriptIndexStateRow] = {}
        eligible_chunk_counts: dict[UUID, int] = {}
        eligible_embedded_counts: dict[UUID, int] = {}
        boundary_by_transcript: dict[UUID, list[TranscriptIndexChunkRow]] = {}
        if transcript_ids:
            turn_rows = (await self.session.execute(
                select(
                    TurnRow.transcript_id,
                    func.min(TurnRow.sequence),
                    func.max(TurnRow.sequence),
                    func.count(TurnRow.id),
                )
                .where(TurnRow.transcript_id.in_(transcript_ids))
                .group_by(TurnRow.transcript_id)
            )).all()
            turn_stats = {
                row[0]: (row[1], row[2], int(row[3] or 0))
                for row in turn_rows
            }
            state_rows = list((await self.session.execute(
                select(TranscriptIndexStateRow).where(
                    TranscriptIndexStateRow.index_version == INDEX_VERSION,
                    TranscriptIndexStateRow.transcript_id.in_(transcript_ids),
                )
            )).scalars())
            states = {row.transcript_id: row for row in state_rows}

            eligible_query = (
                select(TranscriptIndexChunkRow.transcript_id, func.count(TranscriptIndexChunkRow.id))
                .where(
                    TranscriptIndexChunkRow.index_version == INDEX_VERSION,
                    TranscriptIndexChunkRow.transcript_id.in_(transcript_ids),
                )
                .group_by(TranscriptIndexChunkRow.transcript_id)
            )
            eligible_embedded_query = (
                select(TranscriptIndexChunkRow.transcript_id, func.count(TranscriptIndexChunkRow.id))
                .where(
                    TranscriptIndexChunkRow.index_version == INDEX_VERSION,
                    TranscriptIndexChunkRow.transcript_id.in_(transcript_ids),
                    TranscriptIndexChunkRow.embedding.is_not(None),
                )
                .group_by(TranscriptIndexChunkRow.transcript_id)
            )
            if embedding_model:
                eligible_embedded_query = eligible_embedded_query.where(
                    TranscriptIndexChunkRow.embedding_model == embedding_model
                )
            if embedding_dimensions is not None:
                eligible_embedded_query = eligible_embedded_query.where(
                    TranscriptIndexChunkRow.embedding_dimensions == embedding_dimensions
                )
            if before_sequence is not None:
                eligible_query = eligible_query.where(
                    TranscriptIndexChunkRow.end_sequence < before_sequence
                )
                eligible_embedded_query = eligible_embedded_query.where(
                    TranscriptIndexChunkRow.end_sequence < before_sequence
                )
                boundary_rows = list((await self.session.execute(
                    select(TranscriptIndexChunkRow).where(
                        TranscriptIndexChunkRow.index_version == INDEX_VERSION,
                        TranscriptIndexChunkRow.transcript_id.in_(transcript_ids),
                        TranscriptIndexChunkRow.start_sequence < before_sequence,
                        TranscriptIndexChunkRow.end_sequence >= before_sequence,
                    ).order_by(
                        TranscriptIndexChunkRow.transcript_id,
                        TranscriptIndexChunkRow.start_sequence,
                    )
                )).scalars())
                for chunk in boundary_rows:
                    boundary_by_transcript.setdefault(chunk.transcript_id, []).append(chunk)

            eligible_chunk_counts = {
                row[0]: int(row[1] or 0)
                for row in (await self.session.execute(eligible_query)).all()
            }
            eligible_embedded_counts = {
                row[0]: int(row[1] or 0)
                for row in (await self.session.execute(eligible_embedded_query)).all()
            }

        transcript_coverage: list[dict[str, object]] = []
        eligible_chunks_total = 0
        eligible_embedded_total = 0
        for transcript in transcripts:
            canonical_start, canonical_end, turn_count = turn_stats.get(
                transcript.id, (None, None, 0)
            )
            state = states.get(transcript.id)
            indexed_through = int(state.last_indexed_sequence) if state is not None else 0
            requested_start = int(canonical_start) if canonical_start is not None else None
            requested_end = int(canonical_end) if canonical_end is not None else None
            if before_sequence is not None and requested_start is not None:
                if requested_start >= before_sequence:
                    requested_start = None
                    requested_end = None
                elif requested_end is not None:
                    requested_end = min(requested_end, before_sequence - 1)

            eligible_chunks = eligible_chunk_counts.get(transcript.id, 0)
            eligible_embedded = eligible_embedded_counts.get(transcript.id, 0)
            eligible_chunks_total += eligible_chunks
            eligible_embedded_total += eligible_embedded

            requested_range_indexed = (
                requested_end is None or indexed_through >= requested_end
            )
            indexing_gaps: list[dict[str, int]] = []
            if (
                requested_end is not None
                and canonical_start is not None
                and indexed_through < requested_end
            ):
                indexing_gaps.append({
                    "start_sequence": max(int(canonical_start), indexed_through + 1),
                    "end_sequence": requested_end,
                })
            boundary_intersections = [
                {
                    "chunk_id": str(chunk.id),
                    "start_sequence": int(chunk.start_sequence),
                    "end_sequence": int(chunk.end_sequence),
                }
                for chunk in boundary_by_transcript.get(transcript.id, [])[:10]
            ]
            transcript_coverage.append({
                "transcript_id": str(transcript.id),
                "created_at": transcript.created_at.isoformat() if transcript.created_at else None,
                "closed_at": transcript.closed_at.isoformat() if transcript.closed_at else None,
                "canonical_start_sequence": int(canonical_start) if canonical_start is not None else None,
                "canonical_end_sequence": int(canonical_end) if canonical_end is not None else None,
                "canonical_turns": int(turn_count or 0),
                "indexed_through_sequence": indexed_through,
                "active_tail_start_sequence": (
                    indexed_through + 1
                    if canonical_end is not None and indexed_through < int(canonical_end)
                    else None
                ),
                "requested_before_sequence": before_sequence,
                "requested_range_start_sequence": requested_start,
                "requested_range_end_sequence": requested_end,
                "requested_range_indexed": requested_range_indexed,
                "requested_range_boundary_complete": (
                    requested_range_indexed and not boundary_intersections
                ),
                "indexing_gaps": indexing_gaps,
                "straddling_chunks_excluded": boundary_intersections,
                "embedding_coverage": {
                    "eligible_chunks": eligible_chunks,
                    "embedded_chunks": eligible_embedded,
                },
            })

        return {
            "index_version": INDEX_VERSION,
            "chunks": int(chunk_count or 0),
            "embedded_chunks": int(embedded_count or 0),
            "embedding_model": embedding_model,
            "embedding_dimensions": embedding_dimensions,
            "transcripts_with_chunks": int(transcript_count or 0),
            "transcripts_processed": int(state_count or 0),
            "embedding_coverage": {
                "eligible_chunks": eligible_chunks_total,
                "embedded_chunks": eligible_embedded_total,
            },
            "transcripts": transcript_coverage,
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
