from __future__ import annotations

import hashlib
import re
from collections import deque
from collections.abc import Callable
from copy import deepcopy
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from atlas.persistence.models import (
    ContinuityCapsuleRow,
    DurableMemoryRow,
    MemoryCandidateEvidenceRow,
    MemoryCandidateRow,
    MemoryCommandRow,
    MemoryComparisonVerdictRow,
    MemoryConflictRow,
    MemoryDeletionReceiptRow,
    MemoryDiscoveryStateRow,
    MemoryIndependentReadingRow,
    MemoryObligationRow,
    MemoryProvenanceRow,
    MemoryReconciliationAttemptRow,
    MemoryReconciliationRecordRow,
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
from atlas.transcript.models import Actor, TextBlock
from atlas.transcript.repository import TranscriptRepository

from .candidates import (
    MemoryCandidate,
    MemoryCandidateRepository,
    evidence_set_hash,
)
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
from .explicit import (
    EXPLICIT_OBLIGATION_KINDS,
    settle_explicit_obligation,
)

_MEMORY_RESOURCE_TYPE = "memory_state"
_MEMORY_RESOURCE_ID = "owner"
_CONFLICT_DECISIONS = frozenset({"keep_current", "accept_competing", "restate"})
_CONFLICT_RESOLVABLE_STATUSES = frozenset({"open", "expired_unresolved"})
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




def _review_version(*, subject_type: str, subject_id: UUID, text: str, state: str) -> str:
    material = f"{subject_type}:{subject_id}:{state}:{text}".encode()
    return hashlib.sha256(material).hexdigest()


def _owner_directed_kind(record_kind: str | None) -> bool:
    kind = str(record_kind or "")
    return kind == "owner_directed" or kind.startswith("owner_")


def conflict_review_version(
    conflict: MemoryConflictRow,
    target: DurableMemoryRow,
    candidate: MemoryCandidateRow | None,
) -> str:
    """CAS token covering both wordings and every state a resolution depends on."""
    target_text = clean_memory_content(target.content) if target.content else ""
    competing = clean_memory_content(conflict.proposed_content) if conflict.proposed_content else ""
    return _review_version(
        subject_type="memory_conflict",
        subject_id=conflict.id,
        text=f"{target_text}\n---\n{competing}",
        state=(
            f"{conflict.status}:{target.status}:{target.grounding_status}:"
            f"{target.fingerprint or ''}:"
            f"{target.updated_at.isoformat() if target.updated_at else ''}:"
            f"{candidate.status if candidate is not None else ''}:"
            f"{int(candidate.state_version or 0) if candidate is not None else 0}"
        ),
    )


def _turn_text(row: TurnRow | None) -> str | None:
    if row is None or row.deleted_at is not None:
        return None
    parts: list[str] = []
    for block in row.blocks or []:
        if isinstance(block, dict) and block.get("type") == "text":
            value = clean_memory_content(block.get("text"))
            if value:
                parts.append(value)
    return "\n".join(parts) if parts else None

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
        return await self._queue_explicit_content_command(
            operation="remember",
            content=content,
            classification=classification,
            source=source,
        )

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

        target: DurableMemoryRow | None = None
        if memory_raw is not None or old_content is not None:
            async with self.factory() as session:
                repository = DurableMemoryRepository(session)
                if memory_raw is not None:
                    target = await repository.get(UUID(str(memory_raw)))
                    if target is None or target.status == DELETED:
                        return await self._record_explicit_intake_failure(
                            operation="correct",
                            source=source,
                            reason="target memory does not exist",
                        )
                    if target.status != ACTIVE:
                        return await self._record_explicit_intake_failure(
                            operation="correct",
                            source=source,
                            reason="target memory is not active",
                        )
                elif old_content is not None:
                    target = await repository.active_by_fingerprint(
                        memory_fingerprint(old_content)
                    )

        classification = {
            "memory_kind": target.memory_kind if target and target.memory_kind else "fact",
            "scope": target.scope if target else "cross_chat",
            "scope_key": target.scope_key if target else "owner",
            "durability": target.durability if target else "long_term",
            "subject": target.subject if target else None,
            "namespace": target.namespace if target else None,
        }
        return await self._queue_explicit_content_command(
            operation="correct",
            content=new_content,
            classification=classification,
            source=source,
            target_memory_id=(target.id if target is not None else None),
            change_type=change_type,
            legacy_old_content=(old_content if target is None else None),
            suppression_content=(target.content if target is not None else old_content),
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
            preserved_memory_ids: set[UUID] = set()
            if target is not None:
                affected_memory_ids, preserved = await self._memory_descendants(
                    session, {target.id}, never_preserve={target.id}
                )
                preserved_memory_ids.update(preserved)
            affected_rows = []
            if affected_memory_ids:
                affected_rows = list((await session.execute(
                    select(DurableMemoryRow).where(DurableMemoryRow.id.in_(affected_memory_ids))
                )).scalars())

            source_turn_ids: set[UUID] = {
                row.source_turn_id for row in affected_rows if row.source_turn_id is not None
            }
            source_turn_ids.update(
                row.owner_assertion_turn_id
                for row in affected_rows
                if row.owner_assertion_turn_id is not None
            )
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
            if source_candidate_ids:
                source_turn_ids.update(
                    (
                        await session.execute(
                            select(MemoryCandidateEvidenceRow.turn_id).where(
                                MemoryCandidateEvidenceRow.candidate_id.in_(
                                    source_candidate_ids
                                )
                            )
                        )
                    ).scalars()
                )

            redacted_turn_ids: set[UUID] = set()
            if scope == "memory_and_sources" and selected_content:
                redacted_turn_ids = await self._redact_source_turns(
                    session, command_id, selected_content, source_turn_ids
                )
                if source_turn_ids and redacted_turn_ids != source_turn_ids:
                    raise ValueError(
                        "every selected source passage must be isolated safely; retry with the exact source content"
                    )

            dependency_turn_ids = source_turn_ids | redacted_turn_ids
            candidate_ids, evidence_turn_ids = await self._invalidate_candidates(
                session,
                command_id,
                turn_ids=dependency_turn_ids,
                # Exact-content scanning is a legacy cleanup fallback only. Normal
                # V1 purge roots come from persisted turn/candidate dependencies.
                content=(
                    selected_content
                    if not dependency_turn_ids and not source_candidate_ids
                    else None
                ),
                scrub=True,
                candidate_ids=source_candidate_ids,
                redact_content=selected_content,
            )

            # Rebuild the cascade only after the shared resource lock is held and
            # source passages have been resolved. This sees writes that committed
            # before this purge won serialization, including newly published
            # memories and their evidence/candidate provenance.
            if dependency_turn_ids:
                directly_sourced = set(
                    (
                        await session.execute(
                            select(DurableMemoryRow.id).where(
                                or_(
                                    DurableMemoryRow.source_turn_id.in_(dependency_turn_ids),
                                    DurableMemoryRow.owner_assertion_turn_id.in_(dependency_turn_ids),
                                ),
                                DurableMemoryRow.status != DELETED,
                            )
                        )
                    ).scalars()
                )
                provenance_sourced = set(
                    (
                        await session.execute(
                            select(MemoryProvenanceRow.memory_id).where(
                                MemoryProvenanceRow.source_turn_id.in_(dependency_turn_ids)
                            )
                        )
                    ).scalars()
                )
                affected_memory_ids.update(directly_sourced | provenance_sourced)
            affected_memory_ids, preserved = await self._dependent_memory_closure(
                session, affected_memory_ids, candidate_ids,
                never_preserve=({target.id} if target is not None else set()),
            )
            preserved_memory_ids.update(preserved)
            if preserved_memory_ids:
                await self._detach_purged_dependencies(
                    session,
                    preserved_memory_ids,
                    purged_turn_ids=redacted_turn_ids,
                    invalidated_candidate_ids=candidate_ids,
                    deleted_memory_ids=affected_memory_ids,
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
                "preserved_memory_ids": sorted(str(item) for item in preserved_memory_ids),
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

    async def obligations(self, arguments: dict) -> dict[str, object]:
        status = str(arguments.get("status") or "").strip() or None
        kind = str(arguments.get("kind") or "").strip() or None
        limit = max(1, min(int(arguments.get("limit") or 20), 100))
        async with self.factory() as session:
            statement = select(MemoryObligationRow)
            if status is not None:
                statement = statement.where(MemoryObligationRow.status == status)
            if kind is not None:
                statement = statement.where(MemoryObligationRow.kind == kind)
            rows = list((await session.execute(
                statement.order_by(
                    MemoryObligationRow.created_at.desc(), MemoryObligationRow.id.desc()
                ).limit(limit)
            )).scalars())
            projections: list[dict[str, object]] = []
            for row in rows:
                projection = self._project_obligation(row)
                projection.update(await self._review_projection(session, row))
                projections.append(projection)
        return {"obligations": projections}

    async def resolve_obligation(self, arguments: dict) -> dict[str, object]:
        obligation_id = UUID(str(arguments.get("obligation_id")))
        decision = str(arguments.get("decision") or "").strip()
        if decision not in {"confirm", "reject", "retry"} | _CONFLICT_DECISIONS:
            raise ValueError(
                "decision must be confirm, reject, retry, keep_current, accept_competing, or restate"
            )
        review_version = str(arguments.get("review_version") or "").strip() or None
        async with self.factory() as preview_session:
            preview = await preview_session.get(MemoryObligationRow, obligation_id)
            if preview is None:
                raise LookupError("memory obligation not found")
            if preview.kind == "memory_conflict":
                return await self._resolve_memory_conflict(
                    obligation_id,
                    decision=decision,
                    content=arguments.get("content"),
                    change_type=str(arguments.get("change_type") or "correction"),
                    review_version=review_version,
                )
            if decision in _CONFLICT_DECISIONS:
                raise ValueError("conflict decisions apply only to memory conflict obligations")
            if preview.kind == "memory_review" and preview.subject_type == "durable_memory":
                return await self._resolve_memory_review(
                    obligation_id,
                    decision=decision,
                    edited_content=arguments.get("content"),
                    review_version=review_version,
                )

        source = await self._latest_source()
        now = utcnow()
        async with self.factory() as session, session.begin():
            obligation = (await session.execute(
                select(MemoryObligationRow)
                .where(MemoryObligationRow.id == obligation_id)
                .with_for_update()
            )).scalar_one_or_none()
            if obligation is None:
                raise LookupError("memory obligation not found")
            if obligation.status != "pending":
                return {"result": "already_resolved", **self._project_obligation(obligation)}

            if obligation.kind in EXPLICIT_OBLIGATION_KINDS:
                return await self._retry_explicit_obligation(
                    session,
                    obligation,
                    decision=decision,
                    review_version=review_version,
                    source=source,
                    now=now,
                )
            if (
                obligation.kind not in {"memory_confirmation", "memory_review"}
                or obligation.subject_type != "memory_candidate"
            ):
                raise ValueError("this obligation requires a different resolution flow")
            if decision == "retry":
                raise ValueError("candidate confirmation does not support retry")
            candidate = (await session.execute(
                select(MemoryCandidateRow)
                .where(MemoryCandidateRow.id == obligation.subject_id)
                .with_for_update()
            )).scalar_one_or_none()
            if candidate is None:
                raise LookupError("memory candidate for obligation not found")
            current_text = clean_memory_content(candidate.content)
            current_review_version = _review_version(
                subject_type="memory_candidate",
                subject_id=candidate.id,
                text=current_text,
                state=(
                    f"{candidate.status}:{int(candidate.state_version or 0)}:"
                    f"{candidate.fingerprint or ''}:{candidate.evidence_set_hash or ''}"
                ),
            )
            if decision in {"confirm", "reject"} and review_version != current_review_version:
                return {
                    "result": "stale_review",
                    "proposed_assertion_text": current_text,
                    "review_version": current_review_version,
                    **self._project_obligation(obligation),
                }
            if obligation.expires_at is not None and obligation.expires_at <= now:
                candidate.state_version = int(candidate.state_version or 0) + 1
                obligation.status = "resolved"
                obligation.resolution_code = "expired_unconfirmed"
                obligation.resolution_json = {}
                obligation.resolved_at = now
                candidate.status = "expired"
                candidate.processed_at = now
                candidate.decision_json = {
                    "decision": "expired", "code": "owner_confirmation_expired"
                }
                await settle_explicit_obligation(
                    session, candidate, now=now, code="owner_confirmation_expired"
                )
                return {"result": "expired", **self._project_obligation(obligation)}

            candidate.state_version = int(candidate.state_version or 0) + 1
            obligation.status = "resolved"
            obligation.resolved_at = now
            if decision == "confirm":
                asserted = clean_memory_content(
                    arguments.get("content")
                    if arguments.get("content") is not None
                    else candidate.content
                )
                assertion_transcript_id, assertion_turn_id = await self._append_owner_assertion_turn(
                    session, asserted
                )
                evidence_rows = list((await session.execute(
                    select(MemoryCandidateEvidenceRow)
                    .where(MemoryCandidateEvidenceRow.candidate_id == candidate.id)
                    .order_by(MemoryCandidateEvidenceRow.ordinal, MemoryCandidateEvidenceRow.id)
                )).scalars())
                next_ordinal = max((int(row.ordinal) for row in evidence_rows), default=-1) + 1
                session.add(MemoryCandidateEvidenceRow(
                    candidate_id=candidate.id,
                    turn_id=assertion_turn_id,
                    principal="owner",
                    ordinal=next_ordinal,
                    span_ref="text:0",
                ))
                candidate.evidence_set_hash = evidence_set_hash([
                    *((row.turn_id, row.span_ref) for row in evidence_rows),
                    (assertion_turn_id, "text:0"),
                ])
                obligation.resolution_source_transcript_id = assertion_transcript_id
                obligation.resolution_source_turn_id = assertion_turn_id
                obligation.resolution_code = "confirmed"
                obligation.resolution_json = {"candidate_id": str(candidate.id)}
                candidate.status = "pending"
                candidate.lease_token = None
                candidate.leased_until = None
                candidate.review_after = None
                candidate.attempt_count = 0
                candidate.processed_at = None
                candidate.decision_json = {
                    "decision": "owner_confirmed",
                    "owner_confirmation_granted": True,
                    "owner_assertion_turn_id": str(assertion_turn_id),
                    "owner_assertion_transcript_id": str(assertion_transcript_id),
                    "obligation_id": str(obligation.id),
                }
                result = "requeued_for_reconciliation"
            else:
                obligation.resolution_source_transcript_id = source[0]
                obligation.resolution_source_turn_id = source[1]
                obligation.resolution_code = "rejected"
                obligation.resolution_json = {"candidate_id": str(candidate.id)}
                candidate.status = "rejected"
                candidate.lease_token = None
                candidate.leased_until = None
                candidate.review_after = None
                candidate.processed_at = now
                candidate.decision_json = {
                    "decision": "rejected",
                    "code": "owner_rejected_confirmation",
                    "obligation_id": str(obligation.id),
                }
                await settle_explicit_obligation(
                    session, candidate, now=now, code="owner_rejected_confirmation"
                )
                result = "rejected"
            await session.flush()
            return {
                "result": result,
                "candidate_id": str(candidate.id),
                **self._project_obligation(obligation),
            }

    async def _resolve_memory_conflict(
        self,
        obligation_id: UUID,
        *,
        decision: str,
        content: object | None,
        change_type: str,
        review_version: str | None,
    ) -> dict[str, object]:
        """Owner resolution of a memory conflict.

        keep_current retains the target and rejects the competing candidate.
        accept_competing and restate queue an explicit owner correction of the target
        in the same transaction; nothing is written to durable memory directly.
        """
        if decision not in _CONFLICT_DECISIONS:
            raise ValueError("memory conflicts require keep_current, accept_competing, or restate")
        if change_type not in {"correction", "change_over_time"}:
            raise ValueError("invalid change_type")
        source = await self._latest_source()
        operation_id = uuid4()
        envelope = SharedWriteEnvelope(
            operation_id=operation_id,
            resource_type=_MEMORY_RESOURCE_TYPE,
            resource_id=_MEMORY_RESOURCE_ID,
            operation=f"memory_conflict_{decision}",
            expected_version=None,
            payload={"obligation_id": str(obligation_id), "decision": decision},
            actor="owner",
            source_transcript_id=source[0],
            source_turn_id=source[1],
        )
        suppression: list[str] = []

        async def mutate(session: AsyncSession, _version: int) -> SharedWriteMutation:
            obligation = (await session.execute(
                select(MemoryObligationRow)
                .where(MemoryObligationRow.id == obligation_id)
                .with_for_update()
            )).scalar_one_or_none()
            if obligation is None:
                raise LookupError("memory obligation not found")
            if (
                obligation.kind != "memory_conflict"
                or obligation.subject_type != "memory_conflict"
                or obligation.subject_id is None
            ):
                raise ValueError("obligation is not a memory conflict")
            conflict = (await session.execute(
                select(MemoryConflictRow)
                .where(MemoryConflictRow.id == obligation.subject_id)
                .with_for_update()
            )).scalar_one_or_none()
            if conflict is None:
                raise LookupError("memory conflict not found")
            if conflict.status not in _CONFLICT_RESOLVABLE_STATUSES:
                return SharedWriteMutation(
                    mutated=False,
                    result={
                        "result": "already_resolved",
                        "conflict_status": conflict.status,
                        **self._project_obligation(obligation),
                    },
                )
            target = (await session.execute(
                select(DurableMemoryRow)
                .where(DurableMemoryRow.id == conflict.target_memory_id)
                .with_for_update()
            )).scalar_one_or_none()
            candidate = (await session.execute(
                select(MemoryCandidateRow)
                .where(MemoryCandidateRow.id == conflict.candidate_id)
                .with_for_update()
            )).scalar_one_or_none()
            if target is None or target.status != ACTIVE or target.content is None:
                raise ValueError("conflict target is no longer active")
            projection = await self._conflict_projection(session, obligation)
            if review_version != projection["review_version"]:
                return SharedWriteMutation(
                    mutated=False,
                    result={
                        "result": "stale_review",
                        **self._project_obligation(obligation),
                        **projection,
                    },
                )
            now = utcnow()
            target_text = clean_memory_content(target.content)
            obligation.status = "resolved"
            obligation.resolved_at = now
            obligation.resolution_source_transcript_id = source[0]
            obligation.resolution_source_turn_id = source[1]

            if decision == "keep_current":
                conflict.status = "resolved_keep_current"
                conflict.resolved_at = now
                if candidate is not None and candidate.status == "awaiting_owner":
                    candidate.state_version = int(candidate.state_version or 0) + 1
                    candidate.status = "rejected"
                    candidate.lease_token = None
                    candidate.leased_until = None
                    candidate.review_after = None
                    candidate.processed_at = now
                    candidate.decision_json = {
                        "decision": "rejected",
                        "code": "owner_kept_current",
                        "conflict_id": str(conflict.id),
                        "obligation_id": str(obligation.id),
                    }
                    await settle_explicit_obligation(
                        session, candidate, now=now, code="owner_kept_current"
                    )
                session.add(MemoryProvenanceRow(
                    memory_id=target.id,
                    relationship="owner_conflict_kept_current",
                    source_candidate_id=conflict.candidate_id,
                ))
                target.updated_at = now
                obligation.resolution_code = "keep_current"
                obligation.resolution_json = {
                    "conflict_id": str(conflict.id),
                    "target_memory_id": str(target.id),
                    "candidate_id": str(conflict.candidate_id),
                }
                return SharedWriteMutation(result={
                    "result": "keep_current",
                    "conflict_id": str(conflict.id),
                    "target_memory_id": str(target.id),
                    **self._project_obligation(obligation),
                })

            if decision == "restate":
                if content is None:
                    raise ValueError("restate requires content")
                replacement = clean_memory_content(content)
            else:
                if not conflict.proposed_content:
                    raise ValueError(
                        "the competing claim is no longer available; restate the correct wording"
                    )
                replacement = clean_memory_content(conflict.proposed_content)
            if memory_fingerprint(replacement) == target.fingerprint:
                raise ValueError("the wording equals the current memory; use keep_current")
            classification = {
                "memory_kind": target.memory_kind or "fact",
                "scope": target.scope,
                "scope_key": target.scope_key,
                "durability": target.durability,
                "subject": target.subject,
                "namespace": target.namespace,
            }
            queued = await self._queue_explicit_content_command_in(
                session,
                operation="correct",
                content=replacement,
                classification=classification,
                source=source,
                target_memory_id=target.id,
                change_type=change_type,
                suppression_content=target.content,
            )
            queued.pop("_context_suppression", None)
            conflict.status = f"resolved_{decision}"
            conflict.resolved_at = now
            if candidate is not None and candidate.status == "awaiting_owner":
                candidate.state_version = int(candidate.state_version or 0) + 1
                candidate.status = "rejected"
                candidate.lease_token = None
                candidate.leased_until = None
                candidate.review_after = None
                candidate.processed_at = now
                candidate.decision_json = {
                    "decision": "rejected",
                    "code": "owner_resolved_conflict",
                    "conflict_id": str(conflict.id),
                    "obligation_id": str(obligation.id),
                    "superseded_by_command_id": queued["command_id"],
                }
                await settle_explicit_obligation(
                    session, candidate, now=now,
                    code="owner_resolved_conflict", resolution="superseded",
                )
            obligation.resolution_code = decision
            obligation.resolution_json = {
                "conflict_id": str(conflict.id),
                "target_memory_id": str(target.id),
                "candidate_id": str(conflict.candidate_id),
                "command_id": queued["command_id"],
                "correction_candidate_id": queued["candidate_id"],
                "correction_obligation_id": queued["obligation_id"],
                "change_type": change_type,
            }
            suppression.append(target_text)
            return SharedWriteMutation(result={
                "result": "queued_for_verification",
                "status": "queued",
                "operation": "correct",
                "decision": decision,
                "command_id": queued["command_id"],
                "candidate_id": queued["candidate_id"],
                "obligation_id": queued["obligation_id"],
                "conflict_obligation_id": str(obligation.id),
                "conflict_id": str(conflict.id),
                "target_memory_id": str(target.id),
            })

        receipt = await self.writer.execute(envelope, mutate)
        result: dict[str, object] = {
            "operation_id": str(operation_id),
            "memory_revision": receipt.committed_version,
            **receipt.result,
        }
        if suppression and receipt.result.get("result") == "queued_for_verification":
            # Transient provider-context metadata, never part of the durable receipt.
            result["_context_suppression"] = {"contents": list(suppression)}
        return result

    async def pending_owner_attention(
        self,
        transcript_id: UUID | None = None,
        *,
        conflict_limit: int = 5,
        review_limit: int = 5,
    ) -> dict[str, object]:
        """Open conflicts (any chat) and the legacy review backlog, for foreground context."""
        conflict_limit = max(0, min(int(conflict_limit), 16))
        review_limit = max(0, min(int(review_limit), 16))
        async with self.factory() as session:
            conflicts = list((await session.execute(
                select(MemoryConflictRow)
                .where(
                    MemoryConflictRow.status.in_(_CONFLICT_RESOLVABLE_STATUSES),
                    MemoryConflictRow.tombstoned_at.is_(None),
                )
                .order_by(MemoryConflictRow.created_at.desc(), MemoryConflictRow.id.desc())
                .limit(conflict_limit)
            )).scalars()) if conflict_limit else []
            obligations = {
                row.subject_id: row for row in (await session.execute(
                    select(MemoryObligationRow).where(
                        MemoryObligationRow.subject_type == "memory_conflict",
                        MemoryObligationRow.subject_id.in_([row.id for row in conflicts]),
                    )
                )).scalars()
            } if conflicts else {}
            conflict_items: list[dict[str, object]] = []
            for conflict in conflicts:
                obligation = obligations.get(conflict.id)
                if obligation is None:
                    continue
                target = await session.get(DurableMemoryRow, conflict.target_memory_id)
                candidate = await session.get(MemoryCandidateRow, conflict.candidate_id)
                if target is None or target.status != ACTIVE or target.content is None:
                    continue
                conflict_items.append({
                    "obligation_id": str(obligation.id),
                    "conflict_id": str(conflict.id),
                    "target_content": clean_memory_content(target.content),
                    "competing_claim": (
                        clean_memory_content(conflict.proposed_content)
                        if conflict.proposed_content else None
                    ),
                    "reason_code": conflict.reason_code,
                    "status": conflict.status,
                    "raised_in_this_chat": bool(
                        transcript_id is not None
                        and candidate is not None
                        and candidate.source_transcript_id == transcript_id
                    ),
                })
            review_backlog = int((await session.execute(
                select(func.count()).select_from(MemoryObligationRow).where(
                    MemoryObligationRow.kind == "memory_review",
                    MemoryObligationRow.status == "pending",
                )
            )).scalar_one())
            review_items: list[dict[str, object]] = []
            if review_limit and review_backlog:
                reviews = list((await session.execute(
                    select(MemoryObligationRow)
                    .where(
                        MemoryObligationRow.kind == "memory_review",
                        MemoryObligationRow.status == "pending",
                    )
                    .order_by(MemoryObligationRow.created_at.asc(), MemoryObligationRow.id.asc())
                    .limit(review_limit)
                )).scalars())
                for row in reviews:
                    text: str | None = None
                    if row.subject_type == "durable_memory" and row.subject_id is not None:
                        memory = await session.get(DurableMemoryRow, row.subject_id)
                        if memory is not None and memory.content:
                            text = clean_memory_content(memory.content)
                    elif row.subject_type == "memory_candidate" and row.subject_id is not None:
                        candidate = await session.get(MemoryCandidateRow, row.subject_id)
                        if candidate is not None and candidate.content:
                            text = clean_memory_content(candidate.content)
                    review_items.append({
                        "obligation_id": str(row.id),
                        "subject_type": row.subject_type,
                        "subject_id": str(row.subject_id) if row.subject_id else None,
                        "text": text,
                    })
        return {
            "conflicts": conflict_items,
            "review_backlog": review_backlog,
            "reviews": review_items,
        }

    async def _resolve_memory_review(
        self,
        obligation_id: UUID,
        *,
        decision: str,
        edited_content: object | None,
        review_version: str | None,
    ) -> dict[str, object]:
        if decision not in {"confirm", "reject"}:
            raise ValueError("memory review requires confirm or reject")
        operation_id = uuid4()
        envelope = SharedWriteEnvelope(
            operation_id=operation_id,
            resource_type=_MEMORY_RESOURCE_TYPE,
            resource_id=_MEMORY_RESOURCE_ID,
            operation=f"memory_review_{decision}",
            expected_version=None,
            payload={"obligation_id": str(obligation_id), "decision": decision},
            actor="owner",
        )

        async def mutate(session: AsyncSession, _version: int) -> SharedWriteMutation:
            obligation = (await session.execute(
                select(MemoryObligationRow)
                .where(MemoryObligationRow.id == obligation_id)
                .with_for_update()
            )).scalar_one_or_none()
            if obligation is None:
                raise LookupError("memory obligation not found")
            if obligation.status != "pending":
                return SharedWriteMutation(
                    mutated=False,
                    result={"result": "already_resolved", **self._project_obligation(obligation)},
                )
            if obligation.kind != "memory_review" or obligation.subject_type != "durable_memory":
                raise ValueError("obligation is not a durable-memory review")
            if obligation.subject_id is None:
                raise LookupError("memory review target is missing")
            target = (await session.execute(
                select(DurableMemoryRow)
                .where(DurableMemoryRow.id == obligation.subject_id)
                .with_for_update()
            )).scalar_one_or_none()
            if target is None:
                raise LookupError("memory review target not found")
            current_text = clean_memory_content(target.content)
            current_review_version = _review_version(
                subject_type="durable_memory",
                subject_id=target.id,
                text=current_text,
                state=(
                    f"{target.status}:{target.grounding_status}:"
                    f"{target.fingerprint or ''}:{target.updated_at.isoformat() if target.updated_at else ''}"
                ),
            )
            if review_version != current_review_version:
                return SharedWriteMutation(
                    mutated=False,
                    result={
                        "result": "stale_review",
                        "proposed_assertion_text": current_text,
                        "review_version": current_review_version,
                    },
                )
            now = utcnow()
            obligation.status = "resolved"
            obligation.resolved_at = now
            if decision == "reject":
                if target.status == ACTIVE:
                    target.status = RETIRED
                    target.suppresses_recall = True
                    target.retired_at = now
                    target.updated_at = now
                    _clear_embedding(target)
                obligation.resolution_code = "rejected"
                obligation.resolution_json = {"memory_id": str(target.id)}
                return SharedWriteMutation(
                    result={"result": "rejected", "memory_id": str(target.id)}
                )

            asserted = clean_memory_content(
                edited_content if edited_content is not None else target.content
            )
            fingerprint = memory_fingerprint(asserted)
            existing = await DurableMemoryRepository(session).active_by_fingerprint(fingerprint)
            if existing is not None and existing.id != target.id:
                raise ValueError("confirmed review wording already exists as another active memory")
            assertion_transcript_id, assertion_turn_id = await self._append_owner_assertion_turn(
                session, asserted
            )
            target.content = asserted
            target.fingerprint = fingerprint
            target.grounding_status = "verified"
            target.verification_record_id = None
            target.owner_assertion_turn_id = assertion_turn_id
            target.source_transcript_id = assertion_transcript_id
            target.source_turn_id = assertion_turn_id
            target.updated_at = now
            _clear_embedding(target)
            session.add(MemoryProvenanceRow(
                memory_id=target.id,
                relationship="owner_review_confirmed",
                source_turn_id=assertion_turn_id,
            ))
            obligation.resolution_source_transcript_id = assertion_transcript_id
            obligation.resolution_source_turn_id = assertion_turn_id
            obligation.resolution_code = "confirmed"
            obligation.resolution_json = {"memory_id": str(target.id)}
            return SharedWriteMutation(
                result={
                    "result": "confirmed",
                    "memory_id": str(target.id),
                    "owner_assertion_turn_id": str(assertion_turn_id),
                }
            )

        receipt = await self.writer.execute(envelope, mutate)
        return {
            "operation_id": str(operation_id),
            "memory_revision": receipt.committed_version,
            **receipt.result,
        }

    @staticmethod
    async def _append_owner_assertion_turn(
        session: AsyncSession, content: str
    ) -> tuple[UUID, UUID]:
        transcript_repository = TranscriptRepository(session)
        transcript = await transcript_repository.create(kind="memory_review")
        turn = await transcript_repository.append_turn(
            transcript.id, Actor.OWNER, [TextBlock(text=content)]
        )
        transcript_row = await session.get(TranscriptRow, transcript.id)
        assert transcript_row is not None
        transcript_row.closed_at = utcnow()
        transcript_row.updated_at = utcnow()
        return transcript.id, turn.id

    @staticmethod
    async def _conflict_projection(
        session: AsyncSession, row: MemoryObligationRow
    ) -> dict[str, object]:
        """What the owner needs to resolve a conflict: both wordings and a CAS token."""
        empty: dict[str, object] = {"proposed_assertion_text": None, "review_version": None}
        if row.subject_id is None:
            return empty
        conflict = await session.get(MemoryConflictRow, row.subject_id)
        if conflict is None:
            return empty
        target = await session.get(DurableMemoryRow, conflict.target_memory_id)
        candidate = await session.get(MemoryCandidateRow, conflict.candidate_id)
        resolvable = (
            conflict.status in _CONFLICT_RESOLVABLE_STATUSES
            and target is not None
            and target.status == ACTIVE
            and target.content is not None
        )
        return {
            "proposed_assertion_text": None,
            "review_version": (
                conflict_review_version(conflict, target, candidate) if resolvable else None
            ),
            "conflict_id": str(conflict.id),
            "conflict_status": conflict.status,
            "target_memory_id": str(conflict.target_memory_id),
            "target_content": (
                clean_memory_content(target.content)
                if target is not None and target.content else None
            ),
            "target_authority": (
                ("owner_directed" if _owner_directed_kind(target.record_kind) else "derived")
                if target is not None else None
            ),
            "competing_claim": (
                clean_memory_content(conflict.proposed_content)
                if conflict.proposed_content else None
            ),
            "reason_code": conflict.reason_code,
            "candidate_id": str(conflict.candidate_id),
            "candidate_origin": candidate.origin if candidate is not None else None,
            "resolution_options": sorted(_CONFLICT_DECISIONS) if resolvable else [],
        }

    @staticmethod
    async def _review_projection(
        session: AsyncSession, row: MemoryObligationRow
    ) -> dict[str, object]:
        if row.kind == "memory_conflict" and row.subject_type == "memory_conflict":
            return await MemoryLifecycleCommands._conflict_projection(session, row)
        if (
            row.status != "pending"
            or row.subject_id is None
            or row.kind not in ({"memory_review", "memory_confirmation"} | EXPLICIT_OBLIGATION_KINDS)
        ):
            return {"proposed_assertion_text": None, "review_version": None}
        if row.subject_type == "durable_memory":
            target = await session.get(DurableMemoryRow, row.subject_id)
            if target is None or target.content is None:
                return {"proposed_assertion_text": None, "review_version": None}
            text = clean_memory_content(target.content)
            state = (
                f"{target.status}:{target.grounding_status}:"
                f"{target.fingerprint or ''}:{target.updated_at.isoformat() if target.updated_at else ''}"
            )
        elif row.subject_type == "memory_candidate":
            candidate = await session.get(MemoryCandidateRow, row.subject_id)
            if candidate is None or candidate.content is None:
                return {"proposed_assertion_text": None, "review_version": None}
            text = clean_memory_content(candidate.content)
            state = (
                f"{candidate.status}:{int(candidate.state_version or 0)}:"
                f"{candidate.fingerprint or ''}:{candidate.evidence_set_hash or ''}"
            )
        else:
            return {"proposed_assertion_text": None, "review_version": None}
        return {
            "proposed_assertion_text": text,
            "review_version": _review_version(
                subject_type=row.subject_type,
                subject_id=row.subject_id,
                text=text,
                state=state,
            ),
        }

    async def _retry_explicit_obligation(
        self,
        session: AsyncSession,
        obligation: MemoryObligationRow,
        *,
        decision: str,
        review_version: str | None,
        source: tuple[UUID | None, UUID | None],
        now,
    ) -> dict[str, object]:
        if decision != "retry":
            raise ValueError("explicit remember/correct obligations support only retry")
        if obligation.subject_type != "memory_candidate" or obligation.subject_id is None:
            raise ValueError("this explicit command obligation has no retryable candidate")
        candidate = (await session.execute(
            select(MemoryCandidateRow)
            .where(MemoryCandidateRow.id == obligation.subject_id)
            .with_for_update()
        )).scalar_one_or_none()
        if candidate is None:
            raise LookupError("memory candidate for obligation not found")
        projection = self._project_obligation(obligation)
        if candidate.status in {"pending", "leased", "retained_short_term", "awaiting_owner"}:
            return {"result": "already_queued", "candidate_id": str(candidate.id), **projection}
        if candidate.status != "failed":
            return {
                "result": "not_retryable",
                "candidate_id": str(candidate.id),
                "candidate_status": candidate.status,
                **projection,
            }
        current_text = clean_memory_content(candidate.content) if candidate.content else ""
        current_review_version = _review_version(
            subject_type="memory_candidate",
            subject_id=candidate.id,
            text=current_text,
            state=(
                f"{candidate.status}:{int(candidate.state_version or 0)}:"
                f"{candidate.fingerprint or ''}:{candidate.evidence_set_hash or ''}"
            ),
        )
        if review_version != current_review_version:
            return {
                "result": "stale_review",
                "proposed_assertion_text": current_text or None,
                "review_version": current_review_version,
                **projection,
            }
        evidence_turn_ids = set((await session.execute(
            select(MemoryCandidateEvidenceRow.turn_id).where(
                MemoryCandidateEvidenceRow.candidate_id == candidate.id
            )
        )).scalars())
        live_turns = 0
        if evidence_turn_ids:
            live_turns = int((await session.execute(
                select(func.count()).select_from(TurnRow).where(
                    TurnRow.id.in_(evidence_turn_ids), TurnRow.deleted_at.is_(None)
                )
            )).scalar_one())
        if (
            not evidence_turn_ids
            or live_turns != len(evidence_turn_ids)
            or candidate.content is None
            or candidate.fingerprint is None
        ):
            candidate.state_version = int(candidate.state_version or 0) + 1
            candidate.status = "invalidated"
            candidate.invalidated_at = now
            candidate.processed_at = now
            candidate.decision_json = {
                "decision": "invalidated", "code": "evidence_unavailable"
            }
            await settle_explicit_obligation(
                session, candidate, now=now, code="evidence_unavailable"
            )
            return {
                "result": "evidence_unavailable",
                "candidate_id": str(candidate.id),
                **self._project_obligation(obligation),
            }
        owner_retries = int((obligation.resolution_json or {}).get("owner_retries", 0)) + 1
        # Owner retry re-enters the candidate pipeline from persisted evidence.
        # The state_version bump is the CAS boundary: any stale lease holder or
        # publish attempt keyed to the old version loses.
        candidate.state_version = int(candidate.state_version or 0) + 1
        candidate.status = "pending"
        candidate.lease_token = None
        candidate.leased_until = None
        candidate.review_after = None
        candidate.attempt_count = 0
        candidate.processed_at = None
        candidate.decision_json = {
            "decision": "owner_retry",
            "obligation_id": str(obligation.id),
            "owner_retries": owner_retries,
        }
        obligation.resolution_json = {
            **(obligation.resolution_json or {}),
            "owner_retries": owner_retries,
        }
        obligation.resolution_source_transcript_id = source[0]
        obligation.resolution_source_turn_id = source[1]
        await session.flush()
        return {
            "result": "requeued_for_reconciliation",
            "candidate_id": str(candidate.id),
            **self._project_obligation(obligation),
        }

    async def explicit_outcomes_since(
        self,
        transcript_id: UUID,
        *,
        since=None,
        limit: int = 8,
    ) -> list[dict[str, object]]:
        """Resolved explicit remember/correct obligations raised from this chat.

        Used to surface publication outcomes into the next foreground inference
        so the model can report them; nothing here is canonical evidence.
        """
        bounded = max(1, min(int(limit), 32))
        async with self.factory() as session:
            statement = (
                select(MemoryObligationRow)
                .where(
                    MemoryObligationRow.kind.in_(EXPLICIT_OBLIGATION_KINDS),
                    MemoryObligationRow.status == "resolved",
                    MemoryObligationRow.source_transcript_id == transcript_id,
                )
                .order_by(MemoryObligationRow.resolved_at.desc(), MemoryObligationRow.id.desc())
                .limit(bounded)
            )
            if since is not None:
                statement = statement.where(MemoryObligationRow.resolved_at > since)
            rows = list((await session.execute(statement)).scalars())
            outcomes: list[dict[str, object]] = []
            for row in rows:
                resolution = dict(row.resolution_json or {})
                content: str | None = None
                if row.subject_type == "memory_candidate" and row.subject_id is not None:
                    candidate = await session.get(MemoryCandidateRow, row.subject_id)
                    if candidate is not None and candidate.content:
                        content = clean_memory_content(candidate.content)
                command = (
                    await session.get(MemoryCommandRow, row.command_id)
                    if row.command_id is not None else None
                )
                outcomes.append({
                    "obligation_id": str(row.id),
                    "operation": (
                        command.operation if command is not None
                        else ("remember" if row.kind == "explicit_remember" else "correct")
                    ),
                    "resolution_code": row.resolution_code,
                    "content": content,
                    "memory_id": resolution.get("memory_id"),
                    "reason": (
                        resolution.get("reason") or resolution.get("code")
                        or resolution.get("last_error")
                    ),
                    "resolved_at": row.resolved_at.isoformat() if row.resolved_at else None,
                })
            return outcomes

    @staticmethod
    def _project_obligation(row: MemoryObligationRow) -> dict[str, object]:
        return {
            "obligation_id": str(row.id),
            "kind": row.kind,
            "status": row.status,
            "subject_type": row.subject_type,
            "subject_id": str(row.subject_id) if row.subject_id else None,
            "origin": row.origin,
            "resolution_code": row.resolution_code,
            "resolution": dict(row.resolution_json or {}),
            "source_transcript_id": str(row.source_transcript_id) if row.source_transcript_id else None,
            "source_turn_id": str(row.source_turn_id) if row.source_turn_id else None,
            "resolution_source_transcript_id": (
                str(row.resolution_source_transcript_id)
                if row.resolution_source_transcript_id else None
            ),
            "resolution_source_turn_id": (
                str(row.resolution_source_turn_id) if row.resolution_source_turn_id else None
            ),
            "expires_at": row.expires_at.isoformat() if row.expires_at else None,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "resolved_at": row.resolved_at.isoformat() if row.resolved_at else None,
        }

    async def _queue_explicit_content_command(
        self,
        *,
        operation: str,
        content: str,
        classification: dict[str, object],
        source: tuple[UUID | None, UUID | None],
        target_memory_id: UUID | None = None,
        change_type: str | None = None,
        legacy_old_content: str | None = None,
        suppression_content: str | None = None,
    ) -> dict[str, object]:
        if operation not in {"remember", "correct"}:
            raise ValueError("unsupported explicit content command")
        if source[0] is None or source[1] is None:
            return await self._record_explicit_intake_failure(
                operation=operation,
                source=source,
                reason="no canonical owner turn is available for evidence",
            )
        try:
            async with self.factory() as session, session.begin():
                return await self._queue_explicit_content_command_in(
                    session,
                    operation=operation,
                    content=content,
                    classification=classification,
                    source=source,
                    target_memory_id=target_memory_id,
                    change_type=change_type,
                    legacy_old_content=legacy_old_content,
                    suppression_content=suppression_content,
                )
        except Exception as exc:  # noqa: BLE001 - every intake failure must become a visible terminal obligation
            return await self._record_explicit_intake_failure(
                operation=operation,
                source=source,
                reason=f"{type(exc).__name__}: {exc}"[:500],
            )

    async def _queue_explicit_content_command_in(
        self,
        session: AsyncSession,
        *,
        operation: str,
        content: str,
        classification: dict[str, object],
        source: tuple[UUID | None, UUID | None],
        target_memory_id: UUID | None = None,
        change_type: str | None = None,
        legacy_old_content: str | None = None,
        suppression_content: str | None = None,
    ) -> dict[str, object]:
        """Queue an explicit command inside the caller's transaction; raises on any failure."""
        if operation not in {"remember", "correct"}:
            raise ValueError("unsupported explicit content command")
        if source[0] is None or source[1] is None:
            raise ValueError("no canonical owner turn is available for evidence")
        origin = (
            "explicit_owner_request" if operation == "remember"
            else "explicit_owner_correction"
        )
        obligation_kind = (
            "explicit_remember" if operation == "remember" else "explicit_correct"
        )
        stored_arguments: dict[str, object] = {
            "kind": classification["memory_kind"],
            "scope": classification["scope"],
            "durability": classification["durability"],
        }
        if change_type is not None:
            stored_arguments["change_type"] = change_type
        source_turn = await session.get(TurnRow, source[1])
        if (
            source_turn is None
            or source_turn.deleted_at is not None
            or source_turn.actor != "owner"
            or source_turn.transcript_id != source[0]
        ):
            raise ValueError("canonical owner evidence is unavailable")

        command = MemoryCommandRow(
            operation=operation,
            status=PENDING,
            arguments_json=stored_arguments,
            source_transcript_id=source[0],
            source_turn_id=source[1],
            target_memory_id=target_memory_id,
        )
        session.add(command)
        await session.flush()

        candidate_model = MemoryCandidate.model_validate({
            "kind": classification["memory_kind"],
            "content": content,
            "scope": classification["scope"],
            "confidence": 1.0,
            "durability": classification["durability"],
            "proposed_action": "upsert",
            "subject": classification.get("subject"),
            "namespace": classification.get("namespace"),
            "evidence_refs": [{"turn_id": str(source[1]), "span_ref": ""}],
        })
        candidate, _created = await MemoryCandidateRepository(session).enqueue(
            candidate_model,
            source_transcript_id=source[0],
            source_turn_id=source[1],
            source_provider_evidence_id=None,
            allowed_evidence_turn_ids={source[1]},
            proposer_model=None,
            intake_path="explicit_command",
            origin=origin,
        )

        if operation == "correct" and legacy_old_content:
            guard, _ = await DurableMemoryRepository(session).create_guard(
                legacy_old_content,
                status=SUPERSEDED,
                record_kind="owner_correction_guard",
                source_transcript_id=source[0],
                source_turn_id=source[1],
            )
            command.target_memory_id = guard.id

        obligation = MemoryObligationRow(
            kind=obligation_kind,
            status="pending",
            subject_type="memory_candidate",
            subject_id=candidate.id,
            origin=origin,
            command_id=command.id,
            source_transcript_id=source[0],
            source_turn_id=source[1],
        )
        session.add(obligation)
        await session.flush()
        result: dict[str, object] = {
            "command_id": str(command.id),
            "candidate_id": str(candidate.id),
            "obligation_id": str(obligation.id),
            "status": "queued",
            "operation": operation,
            "result": "queued_for_verification",
        }
        if operation == "correct" and suppression_content:
            result["_context_suppression"] = {
                "contents": [clean_memory_content(suppression_content)]
            }
        return result

    async def _record_explicit_intake_failure(
        self,
        *,
        operation: str,
        source: tuple[UUID | None, UUID | None],
        reason: str,
    ) -> dict[str, object]:
        obligation_kind = (
            "explicit_remember" if operation == "remember" else "explicit_correct"
        )
        now = utcnow()
        async with self.factory() as session, session.begin():
            command = MemoryCommandRow(
                operation=operation,
                status=FAILED,
                arguments_json={},
                source_transcript_id=source[0],
                source_turn_id=source[1],
                error=reason,
                failed_at=now,
            )
            session.add(command)
            await session.flush()
            obligation = MemoryObligationRow(
                kind=obligation_kind,
                status="resolved",
                subject_type="memory_command",
                subject_id=command.id,
                origin=(
                    "explicit_owner_request" if operation == "remember"
                    else "explicit_owner_correction"
                ),
                command_id=command.id,
                source_transcript_id=source[0],
                source_turn_id=source[1],
                resolution_code="intake_failed",
                resolution_json={"reason": reason},
                resolved_at=now,
            )
            session.add(obligation)
            await session.flush()
            return {
                "command_id": str(command.id),
                "obligation_id": str(obligation.id),
                "status": "failed",
                "operation": operation,
                "result": "intake_failed",
                "reason": reason,
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
            await session.flush()
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
        now = utcnow()
        command.status = APPLIED
        command.target_memory_id = target_memory_id
        command.replacement_memory_id = replacement_memory_id
        command.applied_at = now
        obligation = (
            await session.execute(
                select(MemoryObligationRow).where(
                    MemoryObligationRow.command_id == command_id,
                    MemoryObligationRow.status == "pending",
                )
            )
        ).scalar_one_or_none()
        if obligation is not None:
            obligation.status = "resolved"
            obligation.resolution_code = "persisted"
            obligation.resolution_json = {
                "target_memory_id": (
                    str(target_memory_id) if target_memory_id is not None else None
                ),
                "replacement_memory_id": (
                    str(replacement_memory_id)
                    if replacement_memory_id is not None
                    else None
                ),
            }
            obligation.resolved_at = now

    async def _fail(self, command_id: UUID, exc: Exception) -> None:
        async with self.factory() as session:
            command = await session.get(MemoryCommandRow, command_id)
            if command is None:
                return
            now = utcnow()
            command.status = FAILED
            command.error = f"{type(exc).__name__}: {exc}"[:2_000]
            command.failed_at = now
            obligation = (
                await session.execute(
                    select(MemoryObligationRow).where(
                        MemoryObligationRow.command_id == command_id,
                        MemoryObligationRow.status == "pending",
                    )
                )
            ).scalar_one_or_none()
            if obligation is not None:
                obligation.status = "resolved"
                obligation.resolution_code = "failed"
                obligation.resolution_json = {}
                obligation.resolved_at = now
            await session.commit()

    async def _independent_owner_grounded_ids(
        self,
        session: AsyncSession,
        memory_ids: set[UUID],
        *,
        never_preserve: set[UUID],
    ) -> set[UUID]:
        eligible = memory_ids - never_preserve
        if not eligible:
            return set()
        rows = list((await session.execute(
            select(DurableMemoryRow).where(
                DurableMemoryRow.id.in_(eligible),
                DurableMemoryRow.status != DELETED,
                DurableMemoryRow.grounding_status == "verified",
                DurableMemoryRow.owner_assertion_turn_id.is_not(None),
            )
        )).scalars())
        assertion_ids = {
            row.owner_assertion_turn_id for row in rows
            if row.owner_assertion_turn_id is not None
        }
        if not assertion_ids:
            return set()
        live_assertions = set((await session.execute(
            select(TurnRow.id).where(
                TurnRow.id.in_(assertion_ids),
                TurnRow.deleted_at.is_(None),
            )
        )).scalars())
        return {
            row.id for row in rows
            if row.owner_assertion_turn_id in live_assertions
        }

    async def _memory_descendants(
        self, session: AsyncSession, initial: set[UUID], *, never_preserve: set[UUID]
    ) -> tuple[set[UUID], set[UUID]]:
        seen = set(initial)
        preserved: set[UUID] = set()
        queue = deque(initial)
        while queue:
            batch = []
            while queue and len(batch) < 100:
                batch.append(queue.popleft())
            candidate_ids = set((await session.execute(
                select(MemoryProvenanceRow.memory_id).where(
                    MemoryProvenanceRow.source_memory_id.in_(batch)
                )
            )).scalars()) - seen - preserved
            barriers = await self._independent_owner_grounded_ids(
                session, candidate_ids, never_preserve=never_preserve
            )
            preserved.update(barriers)
            for memory_id in candidate_ids - barriers:
                if len(seen) >= 1_000:
                    raise ValueError("memory dependency graph exceeds deletion safety bound")
                seen.add(memory_id)
                queue.append(memory_id)
        return seen, preserved

    async def _dependent_memory_closure(
        self,
        session: AsyncSession,
        memory_ids: set[UUID],
        candidate_ids: set[UUID],
        *,
        never_preserve: set[UUID],
    ) -> tuple[set[UUID], set[UUID]]:
        initial_barriers = await self._independent_owner_grounded_ids(
            session, memory_ids, never_preserve=never_preserve
        )
        seen = set(memory_ids) - initial_barriers
        preserved = set(initial_barriers)
        frontier_memories = set(seen)
        frontier_candidates = set(candidate_ids)
        while frontier_memories or frontier_candidates:
            conditions = []
            if frontier_memories:
                conditions.append(MemoryProvenanceRow.source_memory_id.in_(frontier_memories))
            if frontier_candidates:
                conditions.append(MemoryProvenanceRow.source_candidate_id.in_(frontier_candidates))
            if not conditions:
                break
            rows = list((await session.execute(
                select(MemoryProvenanceRow).where(or_(*conditions))
            )).scalars())
            new_memories = {row.memory_id for row in rows} - seen - preserved
            barriers = await self._independent_owner_grounded_ids(
                session, new_memories, never_preserve=never_preserve
            )
            preserved.update(barriers)
            new_memories -= barriers
            if len(seen) + len(new_memories) > 1_000:
                raise ValueError("memory dependency graph exceeds deletion safety bound")
            seen.update(new_memories)
            frontier_memories = new_memories
            frontier_candidates = {
                row.source_candidate_id for row in rows
                if row.source_candidate_id is not None and row.memory_id in new_memories
            }
        return seen, preserved

    async def _detach_purged_dependencies(
        self,
        session: AsyncSession,
        memory_ids: set[UUID],
        *,
        purged_turn_ids: set[UUID],
        invalidated_candidate_ids: set[UUID],
        deleted_memory_ids: set[UUID],
    ) -> None:
        if not memory_ids:
            return
        rows = list((await session.execute(
            select(DurableMemoryRow).where(DurableMemoryRow.id.in_(memory_ids))
        )).scalars())
        assertion_turn_ids = {
            row.owner_assertion_turn_id for row in rows
            if row.owner_assertion_turn_id is not None
        }
        assertion_turns = {}
        if assertion_turn_ids:
            assertion_turns = {
                row.id: row for row in (await session.execute(
                    select(TurnRow).where(TurnRow.id.in_(assertion_turn_ids))
                )).scalars()
            }
        invalid_reconciliation_ids: set[UUID] = set()
        if invalidated_candidate_ids:
            invalid_reconciliation_ids = set((await session.execute(
                select(MemoryReconciliationRecordRow.id).where(
                    MemoryReconciliationRecordRow.candidate_id.in_(invalidated_candidate_ids)
                )
            )).scalars())
        for row in rows:
            assertion = assertion_turns.get(row.owner_assertion_turn_id)
            if assertion is None or assertion.deleted_at is not None:
                continue
            if row.source_turn_id in purged_turn_ids:
                row.source_turn_id = assertion.id
                row.source_transcript_id = assertion.transcript_id
            if row.verification_record_id in invalid_reconciliation_ids:
                row.verification_record_id = None
            row.updated_at = utcnow()

        conditions = []
        if purged_turn_ids:
            conditions.append(MemoryProvenanceRow.source_turn_id.in_(purged_turn_ids))
        if invalidated_candidate_ids:
            conditions.append(
                MemoryProvenanceRow.source_candidate_id.in_(invalidated_candidate_ids)
            )
        if deleted_memory_ids:
            conditions.append(MemoryProvenanceRow.source_memory_id.in_(deleted_memory_ids))
        if conditions:
            await session.execute(
                delete(MemoryProvenanceRow).where(
                    MemoryProvenanceRow.memory_id.in_(memory_ids),
                    or_(*conditions),
                )
            )

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
            # The discovery sweep must re-read from the earliest redacted turn so
            # its window and horizon never rest on content the owner removed.
            discovery = await session.get(MemoryDiscoveryStateRow, transcript_id)
            if discovery is not None:
                discovery.last_scanned_sequence = min(
                    int(discovery.last_scanned_sequence or 0), max(0, sequence - 1)
                )
                discovery.updated_at = utcnow()
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
        redact_content: str | None = None,
    ) -> tuple[set[UUID], set[UUID]]:
        selected: dict[UUID, MemoryCandidateRow] = {}
        selected_ids = set(candidate_ids or set())
        if turn_ids:
            evidence_candidate_ids = set(
                (
                    await session.execute(
                        select(MemoryCandidateEvidenceRow.candidate_id).where(
                            MemoryCandidateEvidenceRow.turn_id.in_(turn_ids)
                        )
                    )
                ).scalars()
            )
            selected_ids.update(evidence_candidate_ids)

        conditions = []
        if turn_ids:
            conditions.append(MemoryCandidateRow.source_turn_id.in_(turn_ids))
        if selected_ids:
            conditions.append(MemoryCandidateRow.id.in_(selected_ids))
        if conditions:
            from sqlalchemy import or_

            rows = list(
                (
                    await session.execute(
                        select(MemoryCandidateRow).where(or_(*conditions))
                    )
                ).scalars()
            )
            selected.update({row.id: row for row in rows})

        if content:
            rows = list(
                (
                    await session.execute(
                        select(MemoryCandidateRow).where(
                            MemoryCandidateRow.content.is_not(None)
                        )
                    )
                ).scalars()
            )
            for row in rows:
                if row.content and content.casefold() in row.content.casefold():
                    selected[row.id] = row

        now = utcnow()
        evidence_turn_ids: set[UUID] = set()
        if scrub:
            for row in selected.values():
                if row.source_provider_evidence_id is None:
                    continue
                evidence_turn = await session.get(
                    TurnRow, row.source_provider_evidence_id
                )
                if evidence_turn is None or evidence_turn.deleted_at is not None:
                    continue
                redacted = deepcopy(evidence_turn.blocks or [])
                changed = False
                for value in (content, redact_content, row.content, row.evidence):
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
            if row.invalidated_at is None:
                row.state_version = int(row.state_version or 0) + 1
            # Purge invalidates the candidate identity regardless of whether it
            # had already reconciled; its semantic result no longer has live
            # evidence and must not appear as a successful current candidate.
            row.status = "invalidated"
            row.lease_token = None
            row.leased_until = None
            row.review_after = None
            row.processed_at = now
            row.invalidated_at = now
            row.invalidation_operation_id = operation_id
            await settle_explicit_obligation(
                session, row, now=now, code="evidence_purged", resolution="evidence_purged"
            )
            if scrub:
                row.content = None
                row.fingerprint = None
                row.evidence = None
                row.decision_json = {}

        ids = set(selected)
        if scrub and ids:
            await self._tombstone_candidate_derivatives(
                session, operation_id, ids, now
            )
        return ids, evidence_turn_ids

    @staticmethod
    async def _tombstone_candidate_derivatives(
        session: AsyncSession,
        operation_id: UUID,
        candidate_ids: set[UUID],
        now,
    ) -> None:
        attempts = list(
            (
                await session.execute(
                    select(MemoryReconciliationAttemptRow).where(
                        MemoryReconciliationAttemptRow.candidate_id.in_(candidate_ids),
                        MemoryReconciliationAttemptRow.completed_at.is_(None),
                    )
                )
            ).scalars()
        )
        for row in attempts:
            row.status = "invalidated"
            row.completed_at = now
            row.error = None
            row.evidence_json = {}
            row.result_json = {"result": "invalidated", "code": "evidence_purged"}

        readings = list(
            (
                await session.execute(
                    select(MemoryIndependentReadingRow).where(
                        MemoryIndependentReadingRow.candidate_id.in_(candidate_ids)
                    )
                )
            ).scalars()
        )
        for row in readings:
            row.extracted_claims_json = []
            row.category = None
            row.scope = None
            row.durability = None
            row.event_valid_from = None
            row.event_valid_to = None
            row.tombstoned_at = now
            row.tombstone_operation_id = operation_id

        comparisons = list(
            (
                await session.execute(
                    select(MemoryComparisonVerdictRow).where(
                        MemoryComparisonVerdictRow.candidate_id.in_(candidate_ids)
                    )
                )
            ).scalars()
        )
        for row in comparisons:
            row.normalized_content = None
            row.category = None
            row.scope = None
            row.durability = None
            row.tombstoned_at = now
            row.tombstone_operation_id = operation_id

        reconciliations = list(
            (
                await session.execute(
                    select(MemoryReconciliationRecordRow).where(
                        MemoryReconciliationRecordRow.candidate_id.in_(candidate_ids)
                    )
                )
            ).scalars()
        )
        for row in reconciliations:
            row.replacement_content = None
            row.tombstoned_at = now
            row.tombstone_operation_id = operation_id

        conflicts = list(
            (
                await session.execute(
                    select(MemoryConflictRow).where(
                        MemoryConflictRow.candidate_id.in_(candidate_ids)
                    )
                )
            ).scalars()
        )
        conflict_ids = {row.id for row in conflicts}
        for row in conflicts:
            row.proposed_content = None
            row.status = "evidence_purged"
            row.tombstoned_at = now
            row.tombstone_operation_id = operation_id
            row.resolved_at = now

        obligations = list(
            (
                await session.execute(
                    select(MemoryObligationRow).where(
                        (
                            (MemoryObligationRow.subject_type == "memory_candidate")
                            & MemoryObligationRow.subject_id.in_(candidate_ids)
                        )
                        | (
                            (MemoryObligationRow.subject_type == "memory_conflict")
                            & MemoryObligationRow.subject_id.in_(conflict_ids)
                        )
                    )
                )
            ).scalars()
        ) if conflict_ids else list(
            (
                await session.execute(
                    select(MemoryObligationRow).where(
                        MemoryObligationRow.subject_type == "memory_candidate",
                        MemoryObligationRow.subject_id.in_(candidate_ids),
                    )
                )
            ).scalars()
        )
        for row in obligations:
            if row.status == "pending":
                row.status = "resolved"
                row.resolution_code = "evidence_purged"
                row.resolved_at = now
            row.resolution_json = {}

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
