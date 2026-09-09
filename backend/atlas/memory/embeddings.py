from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from openai import AsyncOpenAI, OpenAIError
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from atlas.persistence.models import DurableMemoryRow, TranscriptIndexChunkRow

from .indexer import INDEX_VERSION


class EmbeddingError(RuntimeError):
    pass


class EmbeddingClient(Protocol):
    model: str
    dimensions: int

    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


@dataclass
class OpenAIEmbeddingClient:
    api_key: str
    model: str = "text-embedding-3-large"
    dimensions: int = 1_536

    def __post_init__(self) -> None:
        if self.dimensions != 1_536:
            raise ValueError("Atlas transcript semantic index is fixed at 1536 dimensions")
        self.client = AsyncOpenAI(api_key=self.api_key)

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            response = await self.client.embeddings.create(
                model=self.model,
                input=list(texts),
                dimensions=self.dimensions,
                encoding_format="float",
            )
        except OpenAIError as exc:
            raise EmbeddingError(f"Embedding provider request failed: {exc}") from exc
        ordered = sorted(response.data, key=lambda item: item.index)
        vectors = [list(item.embedding) for item in ordered]
        if len(vectors) != len(texts):
            raise EmbeddingError("Embedding provider returned an unexpected result count")
        if any(len(vector) != self.dimensions for vector in vectors):
            raise EmbeddingError("Embedding provider returned an unexpected vector dimension")
        return vectors


class TranscriptEmbeddingIndexer:
    def __init__(self, factory: async_sessionmaker, client: EmbeddingClient) -> None:
        self.factory = factory
        self.client = client

    async def run_once(self, *, batch_size: int = 16, max_chunks: int = 256) -> dict[str, int]:
        remaining = max(0, max_chunks)
        embedded = 0
        batches = 0
        size = max(1, min(batch_size, 64))
        while remaining > 0:
            take = min(size, remaining)
            async with self.factory() as session:
                statement = (
                    select(TranscriptIndexChunkRow.id, TranscriptIndexChunkRow.content)
                    .where(
                        TranscriptIndexChunkRow.index_version == INDEX_VERSION,
                        or_(
                            TranscriptIndexChunkRow.embedding.is_(None),
                            TranscriptIndexChunkRow.embedding_model.is_(None),
                            TranscriptIndexChunkRow.embedding_model != self.client.model,
                            TranscriptIndexChunkRow.embedding_dimensions.is_(None),
                            TranscriptIndexChunkRow.embedding_dimensions != self.client.dimensions,
                        ),
                    )
                    .order_by(TranscriptIndexChunkRow.created_at.asc(), TranscriptIndexChunkRow.id.asc())
                    .limit(take)
                )
                rows = (await session.execute(statement)).all()
            if not rows:
                break

            vectors = await self.client.embed([content for _, content in rows])
            async with self.factory() as session:
                for (chunk_id, _), vector in zip(rows, vectors, strict=True):
                    await session.execute(
                        update(TranscriptIndexChunkRow)
                        .where(TranscriptIndexChunkRow.id == chunk_id)
                        .values(
                            embedding=vector,
                            embedding_model=self.client.model,
                            embedding_dimensions=self.client.dimensions,
                            embedded_at=func.now(),
                        )
                    )
                await session.commit()
            count = len(rows)
            embedded += count
            batches += 1
            remaining -= count
        return {"chunks_embedded": embedded, "embedding_batches": batches}


class DurableMemoryEmbeddingIndexer:
    def __init__(self, factory: async_sessionmaker, client: EmbeddingClient) -> None:
        self.factory = factory
        self.client = client

    async def run_once(
        self, *, batch_size: int = 16, max_memories: int = 256
    ) -> dict[str, int]:
        remaining = max(0, max_memories)
        embedded = 0
        batches = 0
        size = max(1, min(batch_size, 64))
        while remaining > 0:
            take = min(size, remaining)
            async with self.factory() as session:
                statement = (
                    select(DurableMemoryRow.id, DurableMemoryRow.content)
                    .where(
                        DurableMemoryRow.status == "active",
                        DurableMemoryRow.content.is_not(None),
                        or_(
                            DurableMemoryRow.embedding.is_(None),
                            DurableMemoryRow.embedding_model.is_(None),
                            DurableMemoryRow.embedding_model != self.client.model,
                            DurableMemoryRow.embedding_dimensions.is_(None),
                            DurableMemoryRow.embedding_dimensions != self.client.dimensions,
                        )
                    )
                    .order_by(DurableMemoryRow.created_at.asc(), DurableMemoryRow.id.asc())
                    .limit(take)
                )
                rows = (await session.execute(statement)).all()
            if not rows:
                break

            vectors = await self.client.embed([content for _, content in rows])
            async with self.factory() as session:
                for (memory_id, _), vector in zip(rows, vectors, strict=True):
                    await session.execute(
                        update(DurableMemoryRow)
                        .where(DurableMemoryRow.id == memory_id)
                        .values(
                            embedding=vector,
                            embedding_model=self.client.model,
                            embedding_dimensions=self.client.dimensions,
                            embedded_at=func.now(),
                        )
                    )
                await session.commit()
            count = len(rows)
            embedded += count
            batches += 1
            remaining -= count
        return {
            "durable_memories_embedded": embedded,
            "durable_embedding_batches": batches,
        }
