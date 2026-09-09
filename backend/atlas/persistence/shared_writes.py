from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from atlas.persistence.models import (
    SharedResourceVersionRow,
    SharedWriteOperationRow,
)


class IdempotencyConflict(ValueError):
    """An operation id was reused for a different write."""


class SharedWriteEnvelope(BaseModel):
    """Runtime-owned identity and provenance for one shared-state write."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: UUID
    resource_type: str = Field(min_length=1, max_length=64)
    resource_id: str = Field(min_length=1, max_length=255)
    operation: str = Field(min_length=1, max_length=64)
    expected_version: int | None = Field(ge=0)
    payload: dict[str, Any] = Field(default_factory=dict)
    actor: str = Field(min_length=1, max_length=64)
    source_transcript_id: UUID | None = None
    source_turn_id: UUID | None = None
    source_run_id: UUID | None = None


class SharedWriteMutation(BaseModel):
    """Resource-specific mutation result returned inside the write transaction."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mutated: bool = True
    result: dict[str, Any] = Field(default_factory=dict)


class SharedWriteReceipt(BaseModel):
    """Durable result of a shared-state write attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: UUID
    status: str
    outcome: str
    resource_type: str
    resource_id: str
    observed_version: int
    committed_version: int | None
    replayed: bool = False
    result: dict[str, Any] = Field(default_factory=dict)


def _payload_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _receipt(row: SharedWriteOperationRow, *, replayed: bool) -> SharedWriteReceipt:
    return SharedWriteReceipt(
        operation_id=row.id,
        status=row.status,
        outcome=row.outcome,
        resource_type=row.resource_type,
        resource_id=row.resource_id,
        observed_version=row.observed_version,
        committed_version=row.committed_version,
        replayed=replayed,
        result=dict(row.result_json or {}),
    )


Mutation = Callable[[AsyncSession, int], Awaitable[SharedWriteMutation]]


class SharedWriteRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def execute(
        self,
        envelope: SharedWriteEnvelope,
        mutation: Mutation,
    ) -> SharedWriteReceipt:
        payload_hash = _payload_hash(envelope.payload)
        created = await self._claim(envelope, payload_hash)
        if not created:
            row = await self.session.get(SharedWriteOperationRow, envelope.operation_id)
            assert row is not None
            self._assert_same_operation(row, envelope, payload_hash)
            return _receipt(row, replayed=True)

        await self.session.execute(
            insert(SharedResourceVersionRow)
            .values(
                resource_type=envelope.resource_type,
                resource_id=envelope.resource_id,
                version=0,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    SharedResourceVersionRow.resource_type,
                    SharedResourceVersionRow.resource_id,
                ]
            )
        )
        version_row = (
            await self.session.execute(
                select(SharedResourceVersionRow)
                .where(
                    SharedResourceVersionRow.resource_type == envelope.resource_type,
                    SharedResourceVersionRow.resource_id == envelope.resource_id,
                )
                .with_for_update()
            )
        ).scalar_one()
        current_version = version_row.version
        if (
            envelope.expected_version is not None
            and envelope.expected_version != current_version
        ):
            row = await self.session.get(SharedWriteOperationRow, envelope.operation_id)
            assert row is not None
            row.status = "conflict"
            row.outcome = "version_conflict"
            row.observed_version = current_version
            row.committed_version = None
            await self.session.flush()
            return _receipt(row, replayed=False)

        outcome = await mutation(self.session, current_version)
        next_version = current_version + 1 if outcome.mutated else current_version
        if outcome.mutated:
            version_row.version = next_version

        row = await self.session.get(SharedWriteOperationRow, envelope.operation_id)
        assert row is not None
        row.status = "committed"
        row.outcome = "applied" if outcome.mutated else "no_change"
        row.observed_version = current_version
        row.committed_version = next_version
        row.result_json = outcome.result
        await self.session.flush()
        return _receipt(row, replayed=False)

    async def _claim(
        self,
        envelope: SharedWriteEnvelope,
        payload_hash: str,
    ) -> bool:
        statement = (
            insert(SharedWriteOperationRow)
            .values(
                id=envelope.operation_id,
                resource_type=envelope.resource_type,
                resource_id=envelope.resource_id,
                operation=envelope.operation,
                expected_version=envelope.expected_version,
                payload_hash=payload_hash,
                actor=envelope.actor,
                source_transcript_id=envelope.source_transcript_id,
                source_turn_id=envelope.source_turn_id,
                source_run_id=envelope.source_run_id,
                status="pending",
                outcome="pending",
                observed_version=0,
                result_json={},
            )
            .on_conflict_do_nothing(
                index_elements=[SharedWriteOperationRow.id]
            )
            .returning(SharedWriteOperationRow.id)
        )
        return (await self.session.execute(statement)).scalar_one_or_none() is not None

    @staticmethod
    def _assert_same_operation(
        row: SharedWriteOperationRow,
        envelope: SharedWriteEnvelope,
        payload_hash: str,
    ) -> None:
        identity = (
            row.resource_type,
            row.resource_id,
            row.operation,
            row.expected_version,
            row.payload_hash,
            row.actor,
            row.source_transcript_id,
            row.source_turn_id,
            row.source_run_id,
        )
        requested = (
            envelope.resource_type,
            envelope.resource_id,
            envelope.operation,
            envelope.expected_version,
            payload_hash,
            envelope.actor,
            envelope.source_transcript_id,
            envelope.source_turn_id,
            envelope.source_run_id,
        )
        if identity != requested:
            raise IdempotencyConflict(
                "operation_id already belongs to a different shared-state write"
            )


class SharedStateWriter:
    """Owns the transaction that makes a shared-state write durable."""

    def __init__(self, factory: async_sessionmaker) -> None:
        self.factory = factory

    async def execute(
        self,
        envelope: SharedWriteEnvelope,
        mutation: Mutation,
    ) -> SharedWriteReceipt:
        async with self.factory() as session:
            async with session.begin():
                receipt = await SharedWriteRepository(session).execute(
                    envelope,
                    mutation,
                )
            return receipt
