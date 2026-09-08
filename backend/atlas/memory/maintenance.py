from __future__ import annotations

from atlas.config import Settings
from atlas.db import get_session_factory

from .embeddings import OpenAIEmbeddingClient
from .service import MemoryService


def _embedder(settings: Settings) -> OpenAIEmbeddingClient | None:
    api_key = settings.openai_api_key
    if api_key is None:
        return None
    return OpenAIEmbeddingClient(
        api_key=api_key,
        model=settings.memory_embedding_model,
        dimensions=settings.memory_embedding_dimensions,
    )


async def run_memory_index_once(settings: Settings) -> dict[str, object]:
    service = MemoryService(
        get_session_factory(),
        chunk_chars=settings.memory_chunk_chars,
        embedder=_embedder(settings),
        embedding_batch_size=settings.memory_embedding_batch_size,
        embedding_max_chunks_per_run=settings.memory_embedding_max_chunks_per_run,
    )
    return await service.index_once(
        active_tail_exchanges=settings.memory_active_tail_exchanges
    )
