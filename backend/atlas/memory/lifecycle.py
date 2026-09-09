from __future__ import annotations

import re
from collections import deque
from collections.abc import Callable
from copy import deepcopy
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from atlas.persistence.models import (
    ContinuityCapsuleRow,
    DurableMemoryRow,
    MemoryCandidateRow,
    MemoryCommandRow,
    MemoryDeletionReceiptRow,
    MemoryProvenanceRow,
    TranscriptIndexChunkRow,
    TranscriptIndexStateRow,
    TranscriptRow,
    TurnRow,
)
from atlas.persistence.shared_writes import (
    SharedStateWriter,
    SharedWriteEnvelope,
    SharedWriteMutation,
)
from atlas.runtime.invocation import current_transcript_id

from .durable import (
    ACTIVE,
    APPLIED,
    DELETED,
    FAILED,
    PENDING,
    RETIRED,
    SUPERSEDED,
    DurableMemoryRepository,
    MemoryMutationError,
    clean_memory_content,
    memory_fingerprint,
    utcnow,
)

_MEMORY_RESOURCE_TYPE = "memory_state"
_MEMORY_RESOURCE_ID = "owner"
_DELETION_MARKER = "[Content deleted by owner]"
_ALLOWED_KINDS = {
    "identity", "preference", "fact", "decision", "relationship",
    "procedure", "project_state", "intent",
}
_ALLOWED_SCOPES = {"chat", "cross_chat"}
_ALLOWED_DURABILITY = {"short_term", "long_term"}


def _classification(arguments: dict, source_transcript_id: UUID | None) -> dict[str, object]:
    memory_kind = str(arguments.get("kind") or "fact").strip()
    scope = str(arguments.get("scope") or "cross_chat").strip()
    durability = str(arguments.get("durability") or "long_term").strip()
    if memory_kind not in _ALLOWED_KINDS:
        raise ValueError("invalid memory kind")
    if scope not in _ALLOWED_SCOPES:
        raise ValueError("project-scoped durable memory requires a runtime-bound project identity")
    if durability not in _ALLOWED_DURABILITY:
        raise ValueError("invalid memory durability")
    if scope == "chat":
        if source_transcript_id is None:
            raise ValueError("chat-scoped memory requires an owner transcript")
        scope_key = f"chat:{source_transcript_id}"
    else:
        scope_key = "owner"
    subject = " ".join(str(arguments.get("subject") or "").split())[:160] or None
    namespace = " ".join(str(arguments.get("namespace") or "").split())[:160] or None
    return {
        "memory_kind": memory_kind,
        "scope": scope,
        "scope_key": scope_key,
        "durability": durability,
        "subject": subject,
        "namespace": namespace,
    }


def _clear_embedding(row: DurableMemoryRow) -> None:
    row.embedding = None
    row.embedding_model = None
    row.embedding_dimensions = None
    row.embedded_at = None


def _redact_string(value: str, content: str) -> tuple[str, bool]:
    replaced, count = re.subn(re.escape(content), _DELETION_MARKER, value, flags=re.IGNORECASE)
    return replaced, bool(count)


def _redact_value(value: Any, content: str) -> tuple[Any, bool]:
    if isinstance(value, str):
        return _redact_string(value, content)
    if isinstance(value, list):
        changed = False
        items = []
        for item in value:
            redacted, item_changed = _redact_value(item, content)
            items.append(redacted)
            changed = changed or item_changed
        return items, changed
    if isinstance(value, dict):
        changed = False
        result = {}
        for key, item in value.items():
            redacted, item_changed = _redact_value(item, content)
            result[key] = redacted
            changed = changed or item_changed
        return result, changed
    return value, False


class MemoryLifecycleCommands:
    """Owner-directed lifecycle writes, all serialized by one memory-state revision."""

    def __init__(self, factory: async_sessionmaker) -> None:
        self.factory = factory
        self.writer = SharedStateWriter(factory)

    async def remember(self, arguments: dict) -> dict[str, object]:
        content = clean_memory_content(arguments.get("content"))
        source = await self._latest_source()
        classification = _classification(arguments, source[0])
        command_id = await self._begin(
            "remember",
            {
                "kind": classification["memory_kind"],
                "scope": classification["scope"],
                "durability": classification["durability"],
            },
            source,
        )
        fingerprint = memory_fingerprint(content)

        async def mutate(session: AsyncSession, _version: int) -> SharedWriteMutation:
            repository = DurableMemoryRepository(session)
            active = await repository.active_by_fingerprint(fingerprint)
            if active is not None:
                await self._apply_command(session, command_id, replacement_memory_id=active.id)
                return SharedWriteMutation(
                    mutated=False,
                    result={"result": "already_active", "memory_id": str(active.id)},
                )

            retired = await repository.retired_by_fingerprint(fingerprint)
            if retired is not None:
                conflict = await repository.active_by_fingerprint(fingerprint)
                if conflict is not None and conflict.id != retired.id:
                    raise ValueError("an active memory already owns this content")
                retired.status = ACTIVE
                retired.suppresses_recall = False
                retired.retired_at = None
                retired.updated_at = utcnow()
                await repository.invalidate_continuity_capsules()
                await self._apply_command(session, command_id, replacement_memory_id=retired.id)
                return SharedWriteMutation(
                    result={"result": "restored", "memory_id": str(retired.id)}
                )

            memory, _ = await repository.create_active(
                content,
                source_transcript_id=source[0],
                source_turn_id=source[1],
                record_kind="owner_directed",
                **classification,
            )
            if source[1] is not None:
                session.add(MemoryProvenanceRow(
                    memory_id=memory.id,
                    relationship="owner_source",
                    source_turn_id=source[1],
                ))
            await self._apply_command(session, command_id, replacement_memory_id=memory.id)
            return SharedWriteMutation(result={"result": "created", "memory_id": str(memory.id)})

        return await self._run(command_id, source, "remember", mutate, private_content=content)

    async def correct(self, arguments: dict) -> dict[str, object]:
        new_content = clean_memory_content(arguments.get("content"))
        memory_raw = arguments.get("memory_id")
        old_raw = arguments.get("old_content")
        old_content = clean_memory_content(old_raw, "old_content") if old_raw is not None else None
        if memory_raw is None and old_content is None:
            raise ValueError("correct requires memory_id or old_content")
        change_type = str(arguments.get("change_type") or "correction")
        if change_type not in {"correction", "change_over_time"}:
            raise ValueError("invalid change_type")
        source = await self._latest_source()
        stored = {"change_type": change_type, "selector": "memory_id" if memory_raw else "legacy_content"}
        if memory_raw is not None:
            stored["memory_id"] = str(memory_raw)
        command_id = await self._begin("correct", stored, source)
        new_fingerprint = memory_fingerprint(new_content)

        async def mutate(session: AsyncSession, _version: int) -> SharedWriteMutation:
            nonlocal old_content
            repository = DurableMemoryRepository(session)
            target: DurableMemoryRow | None = None
            if memory_raw is not None:
                target = await repository.get(UUID(str(memory_raw)))
                if target is None:
                    raise ValueError("target memory does not exist")
                if target.status == DELETED:
                    raise ValueError("deleted memory is terminal and cannot be corrected")
                if target.status != ACTIVE:
                    raise ValueError("target memory is not active")
                if old_content is not None and target.fingerprint != memory_fingerprint(old_content):
                    raise ValueError("old_content does not match target memory")
            elif old_content is not None:
                target = await repository.active_by_fingerprint(memory_fingerprint(old_content))
                if target is None:
                    target, _ = await repository.create_guard(
                        old_content,
                        status=SUPERSEDED,
                        record_kind="owner_correction_guard",
                        source_transcript_id=source[0],
                        source_turn_id=source[1],
                    )

            assert target is not None
            old_content = target.content
            if target.status == ACTIVE and target.fingerprint == new_fingerprint:
                await self._apply_command(
                    session, command_id, target_memory_id=target.id, replacement_memory_id=target.id
                )
                return SharedWriteMutation(
                    mutated=False,
                    result={"result": "already_current", "memory_id": str(target.id), "target_memory_id": str(target.id)},
                )

            existing = await repository.active_by_fingerprint(new_fingerprint)
            if existing is not None and existing.id != target.id:
                raise ValueError("corrected content already exists as another active memory")

            classification = {
                "record_kind": "owner_directed",
                "memory_kind": target.memory_kind or "fact",
                "scope": target.scope,
                "scope_key": target.scope_key,
                "durability": target.durability,
                "subject": target.subject,
                "namespace": target.namespace,
            }
            now = utcnow()
            replacement, _ = await repository.create_active(
                new_content,
                source_transcript_id=source[0],
                source_turn_id=source[1],
                supersedes_id=target.id,
                valid_from=now if change_type == "change_over_time" else None,
                **classification,
            )
            target.status = SUPERSEDED
            target.suppresses_recall = True
            target.superseded_by_id = replacement.id
            if change_type == "change_over_time":
                target.valid_to = now
            _clear_embedding(target)
            target.updated_at = now
            if source[1] is not None:
                session.add(MemoryProvenanceRow(
                    memory_id=replacement.id,
                    relationship="owner_source",
                    source_turn_id=source[1],
                ))
            session.add(MemoryProvenanceRow(
                memory_id=replacement.id,
                relationship="supersedes",
                source_memory_id=target.id,
            ))
            await self._invalidate_candidates(
                session, command_id, turn_ids={target.source_turn_id} if target.source_turn_id else set(),
                content=target.content, scrub=False,
            )
            await repository.invalidate_continuity_capsules()
            await self._apply_command(
                session,
                command_id,
                target_memory_id=target.id,
                replacement_memory_id=replacement.id,
            )
            return SharedWriteMutation(result={
                "result": "created",
                "memory_id": str(replacement.id),
                "target_memory_id": str(target.id),
            })

        return await self._run(
            command_id, source, "correct", mutate,
            private_content=lambda: old_content,
            include_memory=True,
        )

    async def retire(self, arguments: dict) -> dict[str, object]:
        memory_raw = arguments.get("memory_id")
        content_raw = arguments.get("content")
        content = clean_memory_content(content_raw) if content_raw is not None else None
        if memory_raw is None and content is None:
            raise ValueError("retire requires memory_id or content")
        source = await self._latest_source()
        stored: dict[str, object] = {"selector": "memory_id" if memory_raw else "legacy_content"}
        if memory_raw is not None:
            stored["memory_id"] = str(memory_raw)
        command_id = await self._begin("retire", stored, source)

        async def mutate(session: AsyncSession, _version: int) -> SharedWriteMutation:
            nonlocal content
            repository = DurableMemoryRepository(session)
            target: DurableMemoryRow | None = None
            if memory_raw is not None:
                target = await repository.get(UUID(str(memory_raw)))
                if target is None:
                    raise ValueError("target memory does not exist")
                if target.status == DELETED:
                    raise ValueError("deleted memory is terminal")
                if target.status == RETIRED:
                    content = target.content
                    await self._apply_command(session, command_id, target_memory_id=target.id)
                    return SharedWriteMutation(
                        mutated=False,
                        result={"result": "already_retired", "memory_id": str(target.id)},
                    )
                if target.status != ACTIVE:
                    raise ValueError("only active memory can be retired")
                if content is not None and target.fingerprint != memory_fingerprint(content):
                    raise ValueError("content does not match target memory")
            elif content is not None:
                target = await repository.active_by_fingerprint(memory_fingerprint(content))

            if target is None:
                assert content is not None
                target, created = await repository.create_guard(
                    content,
                    status=RETIRED,
                    record_kind="owner_retire_guard",
                    source_transcript_id=source[0],
                    source_turn_id=source[1],
                )
                result = "retire_guard_created" if created else "already_retired"
            else:
                target.status = RETIRED
                target.suppresses_recall = True
                target.retired_at = utcnow()
                _clear_embedding(target)
                target.updated_at = utcnow()
                result = "retired"

            content = target.content
            await self._invalidate_candidates(
                session, command_id,
                turn_ids={target.source_turn_id} if target.source_turn_id else set(),
                content=target.content,
                scrub=False,
            )
            await repository.invalidate_continuity_capsules()
            await self._apply_command(session, command_id, target_memory_id=target.id)
            return SharedWriteMutation(result={"result": result, "memory_id": str(target.id)})

        return await self._run(command_id, source, "retire", mutate, private_content=lambda: content)

    async def restore(self, arguments: dict) -> dict[str, object]:
        memory_id = UUID(str(arguments.get("memory_id")))
        source = await self._latest_source()
        command_id = await self._begin("restore", {"memory_id": str(memory_id)}, source)

        async def mutate(session: AsyncSession, _version: int) -> SharedWriteMutation:
            repository = DurableMemoryRepository(session)
            target = await repository.get(memory_id)
            if target is None:
                raise ValueError("target memory does not exist")
            if target.status == DELETED:
                raise ValueError("deleted memory is terminal and cannot be restored")
            if target.status == ACTIVE:
                await self._apply_command(session, command_id, replacement_memory_id=target.id)
                return SharedWriteMutation(
                    mutated=False,
                    result={"result": "already_active", "memory_id": str(target.id)},
                )
            if target.status != RETIRED:
                raise ValueError("only retired memory can be restored")
            if not target.fingerprint or not target.content:
                raise ValueError("retired memory payload is unavailable")
            conflict = await repository.active_by_fingerprint(target.fingerprint)
            if conflict is not None and conflict.id != target.id:
                raise ValueError("another active memory already owns this content")
            target.status = ACTIVE
            target.suppresses_recall = False
            target.retired_at = None
            target.updated_at = utcnow()
            await repository.invalidate_continuity_capsules()
            await self._apply_command(session, command_id, replacement_memory_id=target.id)
            return SharedWriteMutation(result={"result": "restored", "memory_id": str(target.id)})

        return await self._run(command_id, source, "restore", mutate, include_memory=True)

    async def delete(self, arguments: dict) -> dict[str, object]:
        memory_raw = arguments.get("memory_id")
        content_raw = arguments.get("content")
        content = clean_memory_content(content_raw) if content_raw is not None else None
        if memory_raw is None and content is None:
            raise ValueError("delete requires memory_id or content")
        scope = str(arguments.get("scope") or "memory_and_sources")
        if scope not in {"memory_only", "memory_and_sources"}:
            raise ValueError("invalid deletion scope")
        source = await self._latest_source()
        stored: dict[str, object] = {"scope": scope, "selector": "memory_id" if memory_raw else "content"}
        if memory_raw is not None:
            stored["memory_id"] = str(memory_raw)
        command_id = await self._begin("delete", stored, source)
        selected_content: str | None = content

        async def mutate(session: AsyncSession, _version: int) -> SharedWriteMutation:
            nonlocal selected_content
            repository = DurableMemoryRepository(session)
            target: DurableMemoryRow | None = None
            if memory_raw is not None:
                target = await repository.get(UUID(str(memory_raw)))
                if target is None:
                    raise ValueError("target memory does not exist")
                if target.status == DELETED:
                    await self._apply_command(session, command_id, target_memory_id=target.id)
                    return SharedWriteMutation(
                        mutated=False,
                        result={"result": "already_deleted", "memory_id": str(target.id)},
                    )
                selected_content = target.content
                if selected_content is None:
                    raise ValueError("target memory content is unavailable")
                if content is not None and target.fingerprint != memory_fingerprint(content):
                    raise ValueError("content does not match target memory")
            else:
                assert content is not None
                fingerprint = memory_fingerprint(content)
                target = await repository.active_by_fingerprint(fingerprint)
                if target is None:
                    target = await repository.retired_by_fingerprint(fingerprint)
                if target is None:
                    target = (
                        await session.execute(
                            select(DurableMemoryRow)
                            .where(
                                DurableMemoryRow.fingerprint == fingerprint,
                                DurableMemoryRow.status != DELETED,
                            )
                            .order_by(DurableMemoryRow.updated_at.desc(), DurableMemoryRow.id.desc())
                            .limit(1)
                        )
                    ).scalar_one_or_none()

            affected_memory_ids: set[UUID] = set()
            if target is not None:
                affected_memory_ids = await self._memory_descendants(session, {target.id})
            affected_rows = []
            if affected_memory_ids:
                affected_rows = list((await session.execute(
                    select(DurableMemoryRow).where(DurableMemoryRow.id.in_(affected_memory_ids))
                )).scalars())

            source_turn_ids: set[UUID] = {
                row.source_turn_id for row in affected_rows if row.source_turn_id is not None
            }
            provenance = []
            if affected_memory_ids:
                provenance = list((await session.execute(
                    select(MemoryProvenanceRow).where(
                        MemoryProvenanceRow.memory_id.in_(affected_memory_ids)
                    )
                )).scalars())
                source_turn_ids.update(
                    item.source_turn_id for item in provenance if item.source_turn_id is not None
                )
            source_candidate_ids = {
                item.source_candidate_id for item in provenance if item.source_candidate_id is not None
            }

            redacted_turn_ids: set[UUID] = set()
            if scope == "memory_and_sources" and selected_content:
                redacted_turn_ids = await self._redact_source_turns(
                    session, command_id, selected_content, source_turn_ids
                )
                if source_turn_ids and redacted_turn_ids != source_turn_ids:
                    raise ValueError(
                        "every selected source passage must be isolated safely; retry with the exact source content"
                    )

            candidate_ids, evidence_turn_ids = await self._invalidate_candidates(
                session,
                command_id,
                turn_ids=source_turn_ids,
                content=selected_content,
                scrub=True,
                candidate_ids=source_candidate_ids,
            )
            affected_memory_ids = await self._dependent_memory_closure(
                session, affected_memory_ids, candidate_ids
            )
            affected_rows = list((await session.execute(
                select(DurableMemoryRow).where(DurableMemoryRow.id.in_(affected_memory_ids))
            )).scalars()) if affected_memory_ids else []
            all_redacted_turn_ids = redacted_turn_ids | evidence_turn_ids
            if all_redacted_turn_ids:
                await self._invalidate_transcript_derivatives(session, all_redacted_turn_ids)

            if selected_content:
                await self._scrub_command_payloads(
                    session,
                    selected_content,
                    affected_memory_ids,
                    source_turn_ids | evidence_turn_ids,
                )

            now = utcnow()
            for row in affected_rows:
                row.status = DELETED
                row.content = None
                row.fingerprint = None
                row.suppresses_recall = True
                _clear_embedding(row)
                row.retired_at = None
                row.deleted_at = now
                row.deletion_operation_id = command_id
                row.updated_at = now

            if target is None:
                target = DurableMemoryRow(
                    status=DELETED,
                    record_kind="owner_delete_tombstone",
                    scope="cross_chat",
                    durability="long_term",
                    content=None,
                    fingerprint=None,
                    suppresses_recall=True,
                    source_transcript_id=source[0],
                    source_turn_id=source[1],
                    deleted_at=now,
                    deletion_operation_id=command_id,
                )
                session.add(target)
                await session.flush()
                affected_memory_ids.add(target.id)

            await repository.invalidate_continuity_capsules()
            affected = {
                "memory_ids": sorted(str(item) for item in affected_memory_ids),
                "turn_ids": sorted(str(item) for item in all_redacted_turn_ids),
                "candidate_ids": sorted(str(item) for item in candidate_ids),
                "memory_count": len(affected_memory_ids),
                "turn_count": len(all_redacted_turn_ids),
                "candidate_count": len(candidate_ids),
            }
            session.add(MemoryDeletionReceiptRow(
                operation_id=command_id,
                target_memory_id=target.id,
                scope=scope,
                affected_json=affected,
            ))
            await self._apply_command(session, command_id, target_memory_id=target.id)
            return SharedWriteMutation(result={
                "result": "deleted",
                "memory_id": str(target.id),
                "scope": scope,
                "affected": affected,
            })

        return await self._run(
            command_id, source, "delete", mutate,
            private_content=lambda: selected_content,
            include_memory=False,
        )

    async def commands(self, arguments: dict) -> dict[str, object]:
        status = str(arguments.get("status") or "").strip() or None
        limit = int(arguments.get("limit") or 20)
        async with self.factory() as session:
            repository = DurableMemoryRepository(session)
            return {
                "commands": await repository.list_commands(limit=limit, status=status),
                "state": await repository.state(),
            }

    async def _latest_source(self) -> tuple[UUID | None, UUID | None]:
        async with self.factory() as session:
            return await DurableMemoryRepository(session).latest_owner_source(
                current_transcript_id.get()
            )

    async def _begin(
        self,
        operation: str,
        stored_arguments: dict[str, object],
        source: tuple[UUID | None, UUID | None],
    ) -> UUID:
        async with self.factory() as session:
            command = MemoryCommandRow(
                operation=operation,
                status=PENDING,
                arguments_json=stored_arguments,
                source_transcript_id=source[0],
                source_turn_id=source[1],
            )
            session.add(command)
            await session.commit()
            return command.id

    async def _run(
        self,
        command_id: UUID,
        source: tuple[UUID | None, UUID | None],
        operation: str,
        mutation,
        *,
        private_content: str | Callable[[], str | None] | None = None,
        include_memory: bool = False,
    ) -> dict[str, object]:
        envelope = SharedWriteEnvelope(
            operation_id=command_id,
            resource_type=_MEMORY_RESOURCE_TYPE,
            resource_id=_MEMORY_RESOURCE_ID,
            operation=operation,
            expected_version=None,
            payload={"command_id": str(command_id), "operation": operation},
            actor="owner",
            source_transcript_id=source[0],
            source_turn_id=source[1],
        )
        try:
            receipt = await self.writer.execute(envelope, mutation)
        except Exception as exc:
            await self._fail(command_id, exc)
            raise MemoryMutationError(
                f"{operation} failed (command_id={command_id}): {exc}"
            ) from exc

        result = {
            "command_id": str(command_id),
            "status": APPLIED,
            "operation": operation,
            "memory_revision": receipt.committed_version,
            **receipt.result,
        }
        memory_id = receipt.result.get("memory_id")
        if include_memory and memory_id:
            async with self.factory() as session:
                memory = await DurableMemoryRepository(session).get(UUID(str(memory_id)))
                if memory is not None:
                    result["memory"] = DurableMemoryRepository.project(memory)
        if operation == "remember" and memory_id:
            async with self.factory() as session:
                memory = await DurableMemoryRepository(session).get(UUID(str(memory_id)))
                if memory is not None:
                    result["memory"] = DurableMemoryRepository.project(memory)
        # Resolve ID-selected text captured under the write lock after commit.
        # It is transient provider-context metadata, never part of the durable receipt.
        suppression = private_content() if callable(private_content) else private_content
        if (suppression and operation in {"correct", "retire", "delete"}
                and receipt.result.get("result") != "already_current"):
            result["_context_suppression"] = {"contents": [suppression]}
        return result

    async def _apply_command(
        self,
        session: AsyncSession,
        command_id: UUID,
        *,
        target_memory_id: UUID | None = None,
        replacement_memory_id: UUID | None = None,
    ) -> None:
        command = await session.get(MemoryCommandRow, command_id)
        assert command is not None
        command.status = APPLIED
        command.target_memory_id = target_memory_id
        command.replacement_memory_id = replacement_memory_id
        command.applied_at = utcnow()

    async def _fail(self, command_id: UUID, exc: Exception) -> None:
        async with self.factory() as session:
            command = await session.get(MemoryCommandRow, command_id)
            if command is None:
                return
            command.status = FAILED
            command.error = f"{type(exc).__name__}: {exc}"[:2_000]
            command.failed_at = utcnow()
            await session.commit()

    async def _memory_descendants(
        self, session: AsyncSession, initial: set[UUID]
    ) -> set[UUID]:
        seen = set(initial)
        queue = deque(initial)
        while queue:
            batch = []
            while queue and len(batch) < 100:
                batch.append(queue.popleft())
            rows = list((await session.execute(
                select(MemoryProvenanceRow.memory_id).where(
                    MemoryProvenanceRow.source_memory_id.in_(batch)
                )
            )).scalars())
            for memory_id in rows:
                if memory_id not in seen:
                    if len(seen) >= 1_000:
                        raise ValueError("memory dependency graph exceeds deletion safety bound")
                    seen.add(memory_id)
                    queue.append(memory_id)
        return seen

    async def _dependent_memory_closure(
        self,
        session: AsyncSession,
        memory_ids: set[UUID],
        candidate_ids: set[UUID],
    ) -> set[UUID]:
        seen = set(memory_ids)
        frontier_memories = set(memory_ids)
        frontier_candidates = set(candidate_ids)
        while frontier_memories or frontier_candidates:
            conditions = []
            if frontier_memories:
                conditions.append(MemoryProvenanceRow.source_memory_id.in_(frontier_memories))
            if frontier_candidates:
                conditions.append(MemoryProvenanceRow.source_candidate_id.in_(frontier_candidates))
            if not conditions:
                break
            from sqlalchemy import or_
            rows = list((await session.execute(
                select(MemoryProvenanceRow).where(or_(*conditions))
            )).scalars())
            new_memories = {row.memory_id for row in rows} - seen
            if len(seen) + len(new_memories) > 1_000:
                raise ValueError("memory dependency graph exceeds deletion safety bound")
            seen.update(new_memories)
            frontier_memories = new_memories
            frontier_candidates = {
                row.source_candidate_id for row in rows
                if row.source_candidate_id is not None and row.memory_id in new_memories
            }
        return seen

    async def _redact_source_turns(
        self,
        session: AsyncSession,
        operation_id: UUID,
        content: str,
        preferred_turn_ids: set[UUID],
    ) -> set[UUID]:
        if preferred_turn_ids:
            rows = list((await session.execute(
                select(TurnRow).where(TurnRow.id.in_(preferred_turn_ids))
            )).scalars())
        else:
            rows = list((await session.execute(
                select(TurnRow).where(TurnRow.actor == "owner")
            )).scalars())
        changed_ids: set[UUID] = set()
        now = utcnow()
        for row in rows:
            redacted, changed = _redact_value(deepcopy(row.blocks or []), content)
            if not changed:
                continue
            row.blocks = redacted
            row.deleted_at = now
            row.deletion_operation_id = operation_id
            changed_ids.add(row.id)
        return changed_ids

    async def _invalidate_transcript_derivatives(
        self, session: AsyncSession, turn_ids: set[UUID]
    ) -> None:
        if not turn_ids:
            return
        turns = list((await session.execute(
            select(TurnRow).where(TurnRow.id.in_(turn_ids))
        )).scalars())
        earliest_turn: dict[UUID, int] = {}
        for turn in turns:
            earliest_turn[turn.transcript_id] = min(
                earliest_turn.get(turn.transcript_id, turn.sequence), turn.sequence
            )

        for transcript_id, sequence in earliest_turn.items():
            # If the edited turn lived inside a multi-turn chunk, rebuilding from
            # sequence-1 would lose the earlier turns that shared that deleted chunk.
            # Reset to the start of the earliest invalidated chunk instead.
            chunk_start = (await session.execute(
                select(func.min(TranscriptIndexChunkRow.start_sequence)).where(
                    TranscriptIndexChunkRow.transcript_id == transcript_id,
                    TranscriptIndexChunkRow.end_sequence >= sequence,
                )
            )).scalar_one_or_none()
            rebuild_from = min(sequence, int(chunk_start)) if chunk_start is not None else sequence
            await session.execute(
                delete(TranscriptIndexChunkRow).where(
                    TranscriptIndexChunkRow.transcript_id == transcript_id,
                    TranscriptIndexChunkRow.end_sequence >= rebuild_from,
                )
            )
            states = list((await session.execute(
                select(TranscriptIndexStateRow).where(
                    TranscriptIndexStateRow.transcript_id == transcript_id
                )
            )).scalars())
            for state in states:
                state.last_indexed_sequence = min(
                    int(state.last_indexed_sequence or 0), max(0, rebuild_from - 1)
                )
            transcript = await session.get(TranscriptRow, transcript_id)
            if transcript is not None:
                transcript.content_revision = int(transcript.content_revision or 0) + 1
                transcript.context_summary = None
                transcript.summarized_through_turn_id = None
                transcript.active_task_state = {}
                transcript.active_task_revision = int(transcript.active_task_revision or 0) + 1
        await session.execute(delete(ContinuityCapsuleRow))

    async def _invalidate_candidates(
        self,
        session: AsyncSession,
        operation_id: UUID,
        *,
        turn_ids: set[UUID],
        content: str | None,
        scrub: bool,
        candidate_ids: set[UUID] | None = None,
    ) -> tuple[set[UUID], set[UUID]]:
        selected: dict[UUID, MemoryCandidateRow] = {}
        if turn_ids or candidate_ids:
            conditions = []
            if turn_ids:
                conditions.append(MemoryCandidateRow.source_turn_id.in_(turn_ids))
            if candidate_ids:
                conditions.append(MemoryCandidateRow.id.in_(candidate_ids))
            if conditions:
                from sqlalchemy import or_
                rows = list((await session.execute(
                    select(MemoryCandidateRow).where(or_(*conditions))
                )).scalars())
                selected.update({row.id: row for row in rows})
        if content:
            rows = list((await session.execute(
                select(MemoryCandidateRow).where(MemoryCandidateRow.content.is_not(None))
            )).scalars())
            for row in rows:
                if row.content and content.casefold() in row.content.casefold():
                    selected[row.id] = row

        now = utcnow()
        evidence_turn_ids: set[UUID] = set()
        if scrub:
            for row in selected.values():
                if row.source_provider_evidence_id is None:
                    continue
                evidence_turn = await session.get(TurnRow, row.source_provider_evidence_id)
                if evidence_turn is None:
                    continue
                redacted = deepcopy(evidence_turn.blocks or [])
                changed = False
                for value in (row.content, row.evidence):
                    if not value:
                        continue
                    redacted, value_changed = _redact_value(redacted, value)
                    changed = changed or value_changed
                if changed:
                    evidence_turn.blocks = redacted
                    evidence_turn.deleted_at = now
                    evidence_turn.deletion_operation_id = operation_id
                    evidence_turn_ids.add(evidence_turn.id)

        for row in selected.values():
            if row.status in {"pending", "leased", "retained_short_term"}:
                row.status = "invalidated"
                row.lease_token = None
                row.leased_until = None
                row.review_after = None
                row.processed_at = now
            row.invalidated_at = now
            row.invalidation_operation_id = operation_id
            if scrub:
                row.content = None
                row.fingerprint = None
                row.evidence = None
                row.decision_json = {}
        return set(selected), evidence_turn_ids

    async def _scrub_command_payloads(
        self,
        session: AsyncSession,
        content: str,
        memory_ids: set[UUID],
        turn_ids: set[UUID],
    ) -> None:
        rows = list((await session.execute(select(MemoryCommandRow))).scalars())
        for row in rows:
            related = (
                row.target_memory_id in memory_ids
                or row.replacement_memory_id in memory_ids
                or row.source_turn_id in turn_ids
            )
            redacted, changed = _redact_value(dict(row.arguments_json or {}), content)
            if related or changed:
                row.arguments_json = redacted if changed else dict(row.arguments_json or {})
                if row.error:
                    row.error, _ = _redact_string(row.error, content)
