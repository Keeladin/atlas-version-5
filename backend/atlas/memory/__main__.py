import asyncio
import json

from sqlalchemy.exc import SQLAlchemyError

from atlas.config import get_settings
from atlas.db import get_session_factory

from .maintenance import run_memory_index_once
from .observer import write_observer_snapshot


async def _main() -> None:
    result = await run_memory_index_once(get_settings())
    try:
        snapshot = await write_observer_snapshot(get_session_factory())
        result.update(observer_status="ready", observer_snapshot=str(snapshot))
    except (LookupError, OSError, SQLAlchemyError, TypeError, ValueError) as exc:
        result.update(observer_status="degraded", observer_error=type(exc).__name__)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(_main())
