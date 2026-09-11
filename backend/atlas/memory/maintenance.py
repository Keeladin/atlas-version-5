from __future__ import annotations

from atlas.config import Settings
from atlas.db import get_session_factory
from atlas.providers.openai import OpenAIProvider

from .continuity import ContinuityCapsuleService
from .discovery import MemoryBackgroundDiscoveryService
from .embeddings import OpenAIEmbeddingClient
from .reconciliation import MemoryReconciliationService
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


def _model(settings: Settings, *, model: str) -> OpenAIProvider:
    api_key = settings.openai_api_key
    if api_key is None:
        raise RuntimeError("OpenAI is not configured")
    return OpenAIProvider(
        api_key=api_key,
        model=model,
        input_token_budget=min(
            settings.working_context_tokens,
            settings.openai_context_window,
        ),
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
        result.update(
            continuity_status="unavailable",
            discovery_status=(
                "unavailable" if settings.memory_reconciliation_enabled else "disabled"
            ),
            reconciliation_status=(
                "unavailable" if settings.memory_reconciliation_enabled else "disabled"
            ),
        )
        return result

    continuity = await ContinuityCapsuleService(
        factory,
        _model(settings, model=settings.openai_model),
        source_turns=settings.memory_continuity_source_turns,
        summary_chars=settings.memory_continuity_summary_chars,
    ).run_once()
    result.update(
        continuity_status="ready" if continuity.failures == 0 else "degraded",
        continuity_transcripts_seen=continuity.transcripts_seen,
        continuity_capsules_created=continuity.capsules_created,
        continuity_failures=continuity.failures,
    )

    if not settings.memory_reconciliation_enabled:
        result.update(discovery_status="disabled", reconciliation_status="disabled")
        return result

    memory_model = _model(
        settings, model=settings.memory_reconciliation_model or settings.openai_model
    )
    discovery = await MemoryBackgroundDiscoveryService(
        factory, memory_model
    ).run_once()
    result.update(
        discovery_status="ready" if discovery.failures == 0 else "degraded",
        discovery_transcripts_seen=discovery.transcripts_seen,
        discovery_turns_scanned=discovery.turns_scanned,
        discovery_accepted=discovery.accepted,
        discovery_duplicates=discovery.duplicates,
        discovery_rejected=discovery.rejected,
        discovery_failures=discovery.failures,
    )

    reconciliation = await MemoryReconciliationService(
        factory,
        memory_model,
        lease_seconds=settings.memory_reconciliation_lease_seconds,
        max_attempts=settings.memory_reconciliation_max_attempts,
        batch_size=settings.memory_reconciliation_batch_size,
        short_term_review_hours=settings.memory_short_term_review_hours,
        short_term_expiry_days=settings.memory_short_term_expiry_days,
    ).run_once()
    result.update(
        reconciliation_status=(
            "ready" if reconciliation.failures == 0 else "degraded"
        ),
        **reconciliation.as_dict(),
    )
    return result
