from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import async_sessionmaker

from .durable import DurableMemoryRepository
from .embeddings import (
    DurableMemoryEmbeddingIndexer,
    EmbeddingClient,
    EmbeddingError,
    TranscriptEmbeddingIndexer,
)
from .indexer import TranscriptIndexer
from .lifecycle import MemoryLifecycleCommands
from .repository import MemorySearchRepository


class MemoryService:
    def __init__(
        self,
        factory: async_sessionmaker,
        *,
        chunk_chars: int = 4_000,
        embedder: EmbeddingClient | None = None,
        embedding_batch_size: int = 16,
        embedding_max_chunks_per_run: int = 256,
    ) -> None:
        self.factory = factory
        self.chunk_chars = chunk_chars
        self.embedder = embedder
        self.embedding_batch_size = embedding_batch_size
        self.embedding_max_chunks_per_run = embedding_max_chunks_per_run
        self.lifecycle_commands = MemoryLifecycleCommands(factory)

    async def search(self, arguments: dict) -> dict[str, object]:
        transcript_raw = arguments.get("transcript_id")
        excludes = arguments.get("exclude_chunk_ids") or []
        query = str(arguments.get("query") or "").strip()
        query_embedding = None
        semantic_status = "lexical_only"
        if self.embedder is not None and query:
            try:
                vectors = await self.embedder.embed([query])
                query_embedding = vectors[0] if vectors else None
                semantic_status = "hybrid" if query_embedding is not None else "lexical_fallback"
            except EmbeddingError:
                semantic_status = "lexical_fallback"

        transcript_id = UUID(str(transcript_raw)) if transcript_raw else None
        before_sequence = (
            int(arguments["before_sequence"])
            if arguments.get("before_sequence") is not None
            else None
        )
        if before_sequence is not None and transcript_id is None:
            raise ValueError("before_sequence requires transcript_id")
        limit = int(arguments.get("limit") or 5)
        include_historical = bool(arguments.get("include_historical") or False)
        async with self.factory() as session:
            durable_repository = DurableMemoryRepository(session)
            from atlas.runtime.invocation import current_transcript_id
            effective_transcript_id = transcript_id or current_transcript_id.get()
            durable_memories = await durable_repository.search_active(
                query,
                limit=limit,
                transcript_id=effective_transcript_id,
                query_embedding=query_embedding,
                embedding_model=self.embedder.model if self.embedder is not None else None,
            )
            historical_memories = (
                await durable_repository.search_historical(
                    query,
                    limit=limit,
                    transcript_id=effective_transcript_id,
                )
                if include_historical
                else []
            )
            memory_state = await durable_repository.state()
            repository = MemorySearchRepository(session)
            results = await repository.search(
                query,
                limit=limit,
                transcript_id=transcript_id,
                before_sequence=before_sequence,
                exclude_chunk_ids=[UUID(str(item)) for item in excludes],
                query_embedding=query_embedding,
                embedding_model=self.embedder.model if self.embedder is not None else None,
            )
            coverage = await repository.coverage(
                transcript_id,
                before_sequence=before_sequence,
                embedding_model=self.embedder.model if self.embedder is not None else None,
                embedding_dimensions=(
                    self.embedder.dimensions if self.embedder is not None else None
                ),
            )
        return {
            "query": query,
            "retrieval": {
                "mode": semantic_status,
                "boundary_policy": (
                    "whole_chunks_strictly_before_sequence"
                    if before_sequence is not None
                    else "unbounded"
                ),
            },
            "memory_policy": {
                "authority": "owner_directed_precedes_derived; lifecycle_and_scope_apply_before_relevance",
                "suppression": "superseded_retired_deleted_sources_are_ineligible_before_transcript_ranking",
                "suppression_guards": int(memory_state["suppression_guards"]),
            },
            "memory_state": memory_state,
            "durable_memories": durable_memories,
            "historical_memories": historical_memories,
            "coverage": coverage,
            "results": results,
        }

    async def remember(self, arguments: dict) -> dict[str, object]:
        return await self.lifecycle_commands.remember(arguments)

    async def correct(self, arguments: dict) -> dict[str, object]:
        return await self.lifecycle_commands.correct(arguments)

    async def retire(self, arguments: dict) -> dict[str, object]:
        return await self.lifecycle_commands.retire(arguments)

    async def restore(self, arguments: dict) -> dict[str, object]:
        return await self.lifecycle_commands.restore(arguments)

    async def delete(self, arguments: dict) -> dict[str, object]:
        return await self.lifecycle_commands.delete(arguments)

    async def commands(self, arguments: dict) -> dict[str, object]:
        return await self.lifecycle_commands.commands(arguments)

    async def index_once(self, *, active_tail_exchanges: int = 10) -> dict[str, object]:
        async with self.factory() as session:
            lexical = await TranscriptIndexer(session, max_chars=self.chunk_chars).run_once(
                active_tail_exchanges=active_tail_exchanges
            )
            await session.commit()
        result: dict[str, object] = {
            "transcripts_seen": lexical.transcripts_seen,
            "transcripts_advanced": lexical.transcripts_advanced,
            "chunks_created": lexical.chunks_created,
            "turns_processed": lexical.turns_processed,
        }
        if self.embedder is None:
            result.update(
                embedding_status="unconfigured",
                chunks_embedded=0,
                embedding_batches=0,
                durable_memories_embedded=0,
                durable_embedding_batches=0,
            )
            return result
        try:
            semantic = await TranscriptEmbeddingIndexer(self.factory, self.embedder).run_once(
                batch_size=self.embedding_batch_size,
                max_chunks=self.embedding_max_chunks_per_run,
            )
            durable_semantic = await DurableMemoryEmbeddingIndexer(
                self.factory, self.embedder
            ).run_once(
                batch_size=self.embedding_batch_size,
                max_memories=self.embedding_max_chunks_per_run,
            )
        except EmbeddingError as exc:
            result.update(
                embedding_status="failed",
                embedding_error=f"{type(exc).__name__}: {exc}",
                chunks_embedded=0,
                embedding_batches=0,
                durable_memories_embedded=0,
                durable_embedding_batches=0,
            )
            return result
        result.update(
            embedding_status="ready",
            embedding_model=self.embedder.model,
            embedding_dimensions=self.embedder.dimensions,
            **semantic,
            **durable_semantic,
        )
        return result
