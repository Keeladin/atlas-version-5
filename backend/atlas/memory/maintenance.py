from __future__ import annotations

from atlas.config import Settings
from atlas.db import get_session_factory

from .service import MemoryService


async def run_memory_index_once(settings: Settings) -> dict[str, int]:
    service = MemoryService(get_session_factory(), chunk_chars=settings.memory_chunk_chars)
    return await service.index_once(
        active_tail_exchanges=settings.memory_active_tail_exchanges
    )
