from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import async_sessionmaker

from .indexer import TranscriptIndexer
from .repository import MemorySearchRepository


class MemoryService:
    def __init__(self, factory: async_sessionmaker, *, chunk_chars: int = 4_000) -> None:
        self.factory = factory
        self.chunk_chars = chunk_chars

    async def search(self, arguments: dict) -> dict[str, object]:
        transcript_raw = arguments.get("transcript_id")
        excludes = arguments.get("exclude_chunk_ids") or []
        async with self.factory() as session:
            repository = MemorySearchRepository(session)
            results = await repository.search(
                str(arguments.get("query") or ""),
                limit=int(arguments.get("limit") or 5),
                transcript_id=UUID(str(transcript_raw)) if transcript_raw else None,
                before_sequence=(
                    int(arguments["before_sequence"])
                    if arguments.get("before_sequence") is not None else None
                ),
                exclude_chunk_ids=[UUID(str(item)) for item in excludes],
            )
            coverage = await repository.coverage(
                UUID(str(transcript_raw)) if transcript_raw else None
            )
        return {"query": arguments.get("query"), "coverage": coverage, "results": results}

    async def index_once(self, *, active_tail_exchanges: int = 10) -> dict[str, int]:
        async with self.factory() as session:
            result = await TranscriptIndexer(session, max_chars=self.chunk_chars).run_once(
                active_tail_exchanges=active_tail_exchanges
            )
            await session.commit()
        return {
            "transcripts_seen": result.transcripts_seen,
            "transcripts_advanced": result.transcripts_advanced,
            "chunks_created": result.chunks_created,
            "turns_processed": result.turns_processed,
        }
