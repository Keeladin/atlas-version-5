from functools import lru_cache

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .config import get_settings


@lru_cache
def get_engine() -> AsyncEngine:
    settings = get_settings()
    return create_async_engine(
        settings.database_dsn,
        pool_pre_ping=True,
        pool_recycle=300,
    )


async def database_health() -> tuple[bool, str | None]:
    try:
        async with get_engine().connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True, None
    except SQLAlchemyError as exc:
        return False, f"{type(exc).__name__}: {exc}"


@lru_cache
def get_session_factory():
    from sqlalchemy.ext.asyncio import async_sessionmaker

    return async_sessionmaker(get_engine(), expire_on_commit=False)
