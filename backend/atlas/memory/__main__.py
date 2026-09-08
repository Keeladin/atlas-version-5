import asyncio
import json

from atlas.config import get_settings

from .maintenance import run_memory_index_once


async def _main() -> None:
    result = await run_memory_index_once(get_settings())
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(_main())
