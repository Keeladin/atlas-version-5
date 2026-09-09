import asyncio
from uuid import uuid4

import pytest
from atlas.persistence.models import (
    RegistryEntryRow,
    SharedResourceVersionRow,
    SharedWriteOperationRow,
)
from atlas.persistence.shared_writes import (
    IdempotencyConflict,
    SharedStateWriter,
    SharedWriteEnvelope,
    SharedWriteMutation,
)
from sqlalchemy import select


def envelope(*, operation_id=None, resource_id="resource-a", expected_version=0, payload=None):
    return SharedWriteEnvelope(
        operation_id=operation_id or uuid4(),
        resource_type="test.resource",
        resource_id=resource_id,
        operation="upsert",
        expected_version=expected_version,
        payload=payload or {"value": "one"},
        actor="runtime-test",
    )


async def add_registry_row(session, _version, *, entry_id: str):
    session.add(
        RegistryEntryRow(
            id=entry_id,
            family="test",
            description="shared write test",
            source="test",
            provisioned=True,
            enabled=False,
            availability="available",
            metadata_json={},
        )
    )
    return SharedWriteMutation(mutated=True, result={"entry_id": entry_id})


@pytest.mark.asyncio
async def test_shared_write_commits_resource_version_and_receipt_atomically(pg_factory):
    writer = SharedStateWriter(pg_factory)
    item = envelope()

    async def mutation(session, version):
        return await add_registry_row(session, version, entry_id="shared-write-applied")

    receipt = await writer.execute(item, mutation)

    assert receipt.status == "committed"
    assert receipt.outcome == "applied"
    assert receipt.observed_version == 0
    assert receipt.committed_version == 1
    assert receipt.replayed is False
    async with pg_factory() as session:
        version = await session.get(
            SharedResourceVersionRow,
            {"resource_type": item.resource_type, "resource_id": item.resource_id},
        )
        operation = await session.get(SharedWriteOperationRow, item.operation_id)
        registry = await session.get(RegistryEntryRow, "shared-write-applied")
        assert version is not None and version.version == 1
        assert operation is not None and operation.status == "committed"
        assert operation.committed_version == 1
        assert registry is not None


@pytest.mark.asyncio
async def test_stale_shared_write_records_conflict_without_running_mutation(pg_factory):
    writer = SharedStateWriter(pg_factory)
    first = envelope(resource_id="stale-resource")

    async def first_mutation(session, version):
        return await add_registry_row(session, version, entry_id="stale-first")

    await writer.execute(first, first_mutation)
    stale = envelope(resource_id="stale-resource", expected_version=0)

    async def must_not_run(_session, _version):
        raise AssertionError("stale mutation executed")

    receipt = await writer.execute(stale, must_not_run)
    assert receipt.status == "conflict"
    assert receipt.outcome == "version_conflict"
    assert receipt.observed_version == 1
    assert receipt.committed_version is None

    async with pg_factory() as session:
        version = await session.get(
            SharedResourceVersionRow,
            {"resource_type": stale.resource_type, "resource_id": stale.resource_id},
        )
        assert version is not None and version.version == 1
        assert await session.get(RegistryEntryRow, "stale-first") is not None


@pytest.mark.asyncio
async def test_shared_write_retry_is_idempotent(pg_factory):
    writer = SharedStateWriter(pg_factory)
    operation_id = uuid4()
    item = envelope(operation_id=operation_id, resource_id="retry-resource")
    calls = 0

    async def mutation(session, version):
        nonlocal calls
        calls += 1
        return await add_registry_row(session, version, entry_id="retry-once")

    first = await writer.execute(item, mutation)
    second = await writer.execute(item, mutation)

    assert first.status == "committed"
    assert second.status == "committed"
    assert second.replayed is True
    assert second.committed_version == 1
    assert calls == 1


@pytest.mark.asyncio
async def test_operation_id_cannot_be_reused_for_different_payload(pg_factory):
    writer = SharedStateWriter(pg_factory)
    operation_id = uuid4()
    first = envelope(
        operation_id=operation_id,
        resource_id="identity-resource",
        payload={"value": "one"},
    )

    async def mutation(session, version):
        return await add_registry_row(session, version, entry_id="identity-first")

    await writer.execute(first, mutation)
    changed = envelope(
        operation_id=operation_id,
        resource_id="identity-resource",
        payload={"value": "two"},
    )
    with pytest.raises(IdempotencyConflict):
        await writer.execute(changed, mutation)


@pytest.mark.asyncio
async def test_concurrent_writers_get_one_commit_and_one_version_conflict(pg_factory):
    writer = SharedStateWriter(pg_factory)
    left = envelope(resource_id="concurrent-resource")
    right = envelope(resource_id="concurrent-resource")

    async def left_mutation(session, version):
        await asyncio.sleep(0.05)
        return await add_registry_row(session, version, entry_id="concurrent-left")

    async def right_mutation(session, version):
        return await add_registry_row(session, version, entry_id="concurrent-right")

    receipts = await asyncio.gather(
        writer.execute(left, left_mutation),
        writer.execute(right, right_mutation),
    )
    statuses = sorted((item.status, item.outcome) for item in receipts)
    assert statuses == [
        ("committed", "applied"),
        ("conflict", "version_conflict"),
    ]

    async with pg_factory() as session:
        version = await session.get(
            SharedResourceVersionRow,
            {"resource_type": left.resource_type, "resource_id": left.resource_id},
        )
        rows = list(
            (
                await session.execute(
                    select(RegistryEntryRow).where(
                        RegistryEntryRow.id.in_(["concurrent-left", "concurrent-right"])
                    )
                )
            ).scalars()
        )
        assert version is not None and version.version == 1
        assert len(rows) == 1


@pytest.mark.asyncio
async def test_committed_no_change_keeps_resource_version(pg_factory):
    writer = SharedStateWriter(pg_factory)
    item = envelope(resource_id="no-change-resource")

    async def mutation(_session, _version):
        return SharedWriteMutation(mutated=False, result={"reason": "already current"})

    receipt = await writer.execute(item, mutation)
    assert receipt.status == "committed"
    assert receipt.outcome == "no_change"
    assert receipt.observed_version == 0
    assert receipt.committed_version == 0

    async with pg_factory() as session:
        version = await session.get(
            SharedResourceVersionRow,
            {"resource_type": item.resource_type, "resource_id": item.resource_id},
        )
        assert version is not None and version.version == 0


@pytest.mark.asyncio
async def test_mutation_exception_rolls_back_operation_and_version(pg_factory):
    writer = SharedStateWriter(pg_factory)
    item = envelope(resource_id="rollback-resource")

    async def mutation(_session, _version):
        raise RuntimeError("local mutation failed")

    with pytest.raises(RuntimeError, match="local mutation failed"):
        await writer.execute(item, mutation)

    async with pg_factory() as session:
        operation = await session.get(SharedWriteOperationRow, item.operation_id)
        version = await session.get(
            SharedResourceVersionRow,
            {"resource_type": item.resource_type, "resource_id": item.resource_id},
        )
        assert operation is None
        assert version is None
