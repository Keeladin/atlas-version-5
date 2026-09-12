from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy.ext.asyncio import async_sessionmaker

from .observability import MemoryObservabilityService

DEFAULT_OBSERVER_PATH = Path("/var/lib/atlas-v5/observer/memory.json")
DEFAULT_OBSERVER_LIMIT = 20


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


async def build_observer_snapshot(
    factory: async_sessionmaker,
    *,
    limit: int = DEFAULT_OBSERVER_LIMIT,
) -> dict[str, object]:
    """Build a bounded read-only projection using the same service as Control."""
    async with factory() as session:
        service = MemoryObservabilityService(session)
        overview = await service.overview(limit=limit)
        details: list[dict[str, object]] = []
        for candidate in overview["recent_candidates"]:
            candidate_id = candidate.get("id")
            if not candidate_id:
                continue
            details.append(await service.candidate_detail(UUID(str(candidate_id))))

    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC),
        "limit": limit,
        "overview": overview,
        "candidate_details": details,
    }


async def write_observer_snapshot(
    factory: async_sessionmaker,
    *,
    path: Path = DEFAULT_OBSERVER_PATH,
    limit: int = DEFAULT_OBSERVER_LIMIT,
) -> Path:
    """Atomically publish a local read-only observer snapshot."""
    snapshot = await build_observer_snapshot(factory, limit=limit)
    payload = json.dumps(
        snapshot,
        sort_keys=True,
        indent=2,
        default=_json_default,
    ) + "\n"

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(payload, encoding="utf-8")
        os.chmod(temporary, 0o640)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return path
