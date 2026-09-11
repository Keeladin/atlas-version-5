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

    # The downgrade must leave a schema that 25a13 can be applied to again.
    await migrate('upgrade', 'head')
    async with engine.begin() as connection:
        assert (
            await connection.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one() == '25a13'
        assert (
            await connection.execute(text(
                "SELECT count(*) FROM memory_obligations "
                "WHERE kind='memory_review' AND status='pending'"
            ))
        ).scalar_one() == 2


@pytest.mark.asyncio
async def test_25a13_downgrade_refuses_after_review_resolution(pg_factory):
    engine = pg_factory.kw["bind"]
    async with engine.begin() as connection:
        schema = (await connection.execute(text("SELECT current_schema()"))).scalar_one()
        await connection.run_sync(Base.metadata.drop_all)
    env = {
        **os.environ,
        "ATLAS_DATABASE_URL": os.environ["ATLAS_TEST_DATABASE_URL"],
        "PGOPTIONS": f"-csearch_path={schema},public",
        "PYTHONPATH": str(Path.cwd() / "backend"),
    }
    env.pop("ATLAS_DATABASE_URL_FILE", None)

    async def migrate(*args: str, expect_ok: bool = True) -> subprocess.CompletedProcess[str]:
        result = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "alembic", *args],
            env=env,
            capture_output=True,
            text=True,
        )
        if expect_ok:
            assert result.returncode == 0, result.stdout + result.stderr
        return result

    await migrate("upgrade", "25a12")
    memory_id = uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO durable_memories "
                "(id,status,record_kind,content,fingerprint,suppresses_recall,scope,durability) "
                "VALUES (:id,'active','owner_directed','legacy owner row','legacy-owner-fp',false,'cross_chat','long_term')"
            ),
            {"id": memory_id},
        )
    await migrate("upgrade", "25a13")
    async with engine.begin() as connection:
        review_id = (await connection.execute(
            text(
                "SELECT id FROM memory_obligations "
                "WHERE kind='memory_review' AND subject_id=:id"
            ),
            {"id": memory_id},
        )).scalar_one()
        await connection.execute(
            text(
                "UPDATE memory_obligations SET status='resolved', "
                "resolution_code='confirmed', resolved_at=now() WHERE id=:id"
            ),
            {"id": review_id},
        )

    result = await migrate("downgrade", "25a12", expect_ok=False)
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "downgrade refused" in combined

    async with engine.begin() as connection:
        current = (await connection.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
        assert current == "25a13"
        row = (await connection.execute(
            text("SELECT grounding_status, origin FROM durable_memories WHERE id=:id"),
            {"id": memory_id},
        )).one()
        assert row.grounding_status == "legacy_unverified"
        assert row.origin == "legacy_pre25a13"
