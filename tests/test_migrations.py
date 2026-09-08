import re
from pathlib import Path


def _metadata(path: Path) -> tuple[str, str | None]:
    text = path.read_text()
    revision_match = re.search(r'^revision:.*?= ["\']([^"\']+)["\']', text, re.MULTILINE)
    assert revision_match is not None, f"missing revision in {path}"
    down_match = re.search(r'^down_revision:.*?= (None|["\'][^"\']+["\'])', text, re.MULTILINE)
    assert down_match is not None, f"missing down_revision in {path}"
    raw = down_match.group(1)
    return revision_match.group(1), None if raw == "None" else raw.strip("\'\"")


def test_alembic_history_is_one_connected_linear_chain() -> None:
    versions = Path("migrations/versions")
    metadata = dict(_metadata(path) for path in versions.glob("*.py"))
    assert len(metadata) == len(list(versions.glob("*.py")))
    roots = [revision for revision, parent in metadata.items() if parent is None]
    parents = {parent for parent in metadata.values() if parent is not None}
    heads = [revision for revision in metadata if revision not in parents]
    assert len(roots) == 1
    assert len(heads) == 1

    seen = set()
    current = heads[0]
    while current is not None:
        assert current not in seen, "migration history contains a cycle"
        seen.add(current)
        current = metadata[current]
    assert seen == set(metadata), "migration history contains a disconnected branch"


def test_migration_history_contains_execution_reconciliation_state() -> None:
    texts = [path.read_text() for path in Path("migrations/versions").glob("*.py")]
    assert any("execution_started_at" in text for text in texts)


def test_migration_history_contains_active_task_state() -> None:
    texts = [path.read_text() for path in Path("migrations/versions").glob("*.py")]
    assert any("active_task_state" in text for text in texts)


def test_latest_migration_adds_owner_chat_metadata() -> None:
    metadata = {revision: (parent, path) for path in Path("migrations/versions").glob("*.py") for revision, parent in [_metadata(path)]}
    parents = {parent for parent, _ in metadata.values() if parent is not None}
    head = next(revision for revision in metadata if revision not in parents)
    text = metadata[head][1].read_text()
    assert head == "25a07"
    assert 'op.add_column("transcripts", sa.Column("title"' in text
    assert '"updated_at"' in text


import asyncio
import os
import subprocess
import sys
from uuid import uuid4

import pytest
from atlas.persistence.models import Base, RunRow, TranscriptRow, TurnRow
from sqlalchemy import select, text


@pytest.mark.asyncio
@pytest.mark.parametrize('upgrade_existing', [False, True])
async def test_migrations_on_postgresql_preserve_history(pg_factory, upgrade_existing):
    engine = pg_factory.kw['bind']
    async with engine.begin() as connection:
        schema = (await connection.execute(text('SELECT current_schema()'))).scalar_one()
        await connection.run_sync(Base.metadata.drop_all)
    env = {**os.environ, 'ATLAS_DATABASE_URL': os.environ['ATLAS_TEST_DATABASE_URL'],
        'PGOPTIONS': f'-csearch_path={schema},public'}
    env.pop('ATLAS_DATABASE_URL_FILE', None)
    async def migrate(target):
        result = await asyncio.to_thread(subprocess.run,
            [sys.executable, '-m', 'alembic', 'upgrade', target], env=env, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    if upgrade_existing:
        await migrate('f4a7c91d2e30')
        first, second, run_id = uuid4(), uuid4(), uuid4()
        async with engine.begin() as connection:
            await connection.execute(text("INSERT INTO transcripts (id, kind, active_task_state) VALUES (:id, 'owner', CAST(:state AS jsonb)), (:second, 'owner', '{}'::jsonb)"),
                {'id': first, 'second': second, 'state': '{"status":"active","semantic":{"objective":"Keep this"}}'})
            for index in range(3):
                await connection.execute(text("INSERT INTO turns (id, transcript_id, actor, blocks) VALUES (:id, :transcript, 'owner', '[]'::jsonb)"),
                    {'id': uuid4(), 'transcript': first})
            await connection.execute(text("INSERT INTO runs (id, kind, status, transcript_id, intent) VALUES (:id, 'foreground', 'succeeded', :transcript, 'Old completed run')"),
                {'id': run_id, 'transcript': first})
    await migrate('head')
    checked = await asyncio.to_thread(subprocess.run, [sys.executable, '-m', 'alembic', 'check'],
        env=env, capture_output=True, text=True, check=False)
    assert checked.returncode == 0, checked.stdout + checked.stderr
    async with pg_factory() as session:
        assert (await session.execute(text('SELECT version_num FROM alembic_version'))).scalar_one() == '25a07'
        if upgrade_existing:
            rows = (await session.execute(select(TranscriptRow))).scalars().all()
            assert len(rows) == 2 and sum(row.closed_at is None for row in rows) == 1
            active = next(row for row in rows if row.closed_at is None)
            assert active.title == 'Atlas'
            assert all(row.title and row.updated_at for row in rows)
            checkpoint = (await session.get(TranscriptRow, first)).active_task_state
            assert checkpoint['semantic']['objective'] == 'Keep this'
            assert checkpoint['task_id'] and checkpoint['revision'] == 0
            assert list((await session.execute(select(TurnRow.sequence).order_by(TurnRow.sequence))).scalars()) == [1, 2, 3]
            assert (await session.get(RunRow, run_id)).inference_status == 'succeeded'
