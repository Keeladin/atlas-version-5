from __future__ import annotations

from atlas.config import Settings
from atlas.db import get_session_factory
from atlas.providers.openai import OpenAIProvider

from .continuity import ContinuityCapsuleService
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
    factory = get_session_factory()
    service = MemoryService(
        factory,
        chunk_chars=settings.memory_chunk_chars,
        embedder=_embedder(settings),
        embedding_batch_size=settings.memory_embedding_batch_size,
        embedding_max_chunks_per_run=settings.memory_embedding_max_chunks_per_run,
    )
    result = await service.index_once(
        active_tail_exchanges=settings.memory_active_tail_exchanges
    )
    api_key = settings.openai_api_key
    if api_key is None:
        return {**result, "continuity_status": "unavailable"}
    continuity = await ContinuityCapsuleService(
        factory,
        OpenAIProvider(
            api_key=api_key,
            model=settings.openai_model,
            input_token_budget=min(settings.working_context_tokens, settings.openai_context_window),
        ),
        source_turns=settings.memory_continuity_source_turns,
        summary_chars=settings.memory_continuity_summary_chars,
    ).run_once()
    return {
        **result,
        "continuity_status": "ready" if continuity.failures == 0 else "degraded",
        "continuity_transcripts_seen": continuity.transcripts_seen,
        "continuity_capsules_created": continuity.capsules_created,
        "continuity_failures": continuity.failures,
    }
