from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from atlas.db import get_session_factory


async def get_session() -> AsyncIterator[AsyncSession]:
    factory = get_session_factory()
    async with factory() as session:
        yield session
