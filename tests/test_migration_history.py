import asyncio
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from atlas.persistence.models import Base
from sqlalchemy import text


@pytest.mark.asyncio
async def test_25a13_marks_all_existing_memories_unverified_and_downgrades(pg_factory):
    engine = pg_factory.kw['bind']
    async with engine.begin() as connection:
        schema = (await connection.execute(text('SELECT current_schema()'))).scalar_one()
        await connection.run_sync(Base.metadata.drop_all)
    env = {
        **os.environ,
        'ATLAS_DATABASE_URL': os.environ['ATLAS_TEST_DATABASE_URL'],
        'PGOPTIONS': f'-csearch_path={schema},public',
        'PYTHONPATH': str(Path.cwd() / 'backend'),
    }
    env.pop('ATLAS_DATABASE_URL_FILE', None)

    async def migrate(*args: str) -> None:
        result = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, '-m', 'alembic', *args],
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    await migrate('upgrade', '25a12')
    owner_id, derived_id = uuid4(), uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO durable_memories "
                "(id,status,record_kind,content,fingerprint,suppresses_recall,scope,durability) "
                "VALUES (:owner,'active','owner_directed','owner legacy','owner-fp',false,'cross_chat','long_term'), "
                "(:derived,'active','derived','derived legacy','derived-fp',false,'cross_chat','long_term')"
            ),
            {'owner': owner_id, 'derived': derived_id},
        )

    await migrate('upgrade', 'head')
    async with engine.begin() as connection:
        rows = (
            await connection.execute(
                text(
                    "SELECT id, origin, grounding_status FROM durable_memories "
                    "WHERE id IN (:owner, :derived) ORDER BY id"
                ),
                {'owner': owner_id, 'derived': derived_id},
            )
        ).all()
        assert len(rows) == 2
        assert {row.origin for row in rows} == {'legacy_pre25a13'}
        assert {row.grounding_status for row in rows} == {'legacy_unverified'}

        reviews = (
            await connection.execute(
                text(
                    "SELECT subject_id, origin, status FROM memory_obligations "
                    "WHERE kind='memory_review' ORDER BY subject_id"
                )
            )
        ).all()
        assert {row.subject_id for row in reviews} == {owner_id, derived_id}
        assert {row.origin for row in reviews} == {'legacy_pre25a13'}
        assert {row.status for row in reviews} == {'pending'}

    await migrate('downgrade', '25a12')
    async with engine.begin() as connection:
        assert (
            await connection.execute(
                text("SELECT count(*) FROM durable_memories WHERE id IN (:owner, :derived)"),
                {'owner': owner_id, 'derived': derived_id},
            )
        ).scalar_one() == 2
        columns = {
            row.column_name
            for row in (
                await connection.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema=current_schema() AND table_name='durable_memories'"
                    )
                )
            )
        }
        assert 'grounding_status' not in columns
        assert 'origin' not in columns
