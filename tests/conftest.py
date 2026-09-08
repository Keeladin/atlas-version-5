import os
from uuid import uuid4

import pytest
import pytest_asyncio
from atlas.persistence.models import Base
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest_asyncio.fixture
async def pg_factory():
    """Real PostgreSQL in an isolated schema; never fall back to the app database."""
    url = os.environ.get('ATLAS_TEST_DATABASE_URL')
    if not url:
        pytest.skip('Set ATLAS_TEST_DATABASE_URL to a disposable PostgreSQL database')
    admin = create_async_engine(url)
    schema = 'test_' + uuid4().hex
    async with admin.begin() as connection:
        await connection.execute(text('CREATE EXTENSION IF NOT EXISTS vector'))
        await connection.execute(text(f'CREATE SCHEMA {schema}'))
    engine = create_async_engine(url, connect_args={'options': f'-csearch_path={schema},public'})
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA {schema} CASCADE'))
        await admin.dispose()
