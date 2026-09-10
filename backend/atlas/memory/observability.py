from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from atlas.persistence.models import (
    DurableMemoryRow,
    MemoryCandidateRow,
    MemoryProvenanceRow,
    MemoryReconciliationAttemptRow,
    TranscriptRow,
    TurnRow,
)


def _turn_text(row: TurnRow | None, limit: int = 4_000) -> str | None:
    if row is None or row.deleted_at is not None:
        return None
    parts: list[str] = []
    for block in row.blocks or []:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        value = " ".join(str(block.get("text") or "").split()).strip()
        if value:
            parts.append(value)
    text = "\n".join(parts).strip()
    return text[:limit] if text else None


def _attempt_projection(row: MemoryReconciliationAttemptRow) -> dict[str, object]:
    return {
        "id": str(row.id),
        "attempt_number": int(row.attempt_number),
        "status": row.status,
        "semantic_decision": row.semantic_decision,
        "target_memory_id": str(row.target_memory_id) if row.target_memory_id else None,
        "operation_id": str(row.operation_id) if row.operation_id else None,
        "evaluated_memory_revision": row.evaluated_memory_revision,
        "evaluated_source_revision": row.evaluated_source_revision,
        "evidence_refs": row.evidence_json or {},
        "result": row.result_json or {},
        "error": row.error,
        "started_at": row.started_at,
        "completed_at": row.completed_at,
    }


def _candidate_projection(
    row: MemoryCandidateRow,
    *,
    latest_attempt: MemoryReconciliationAttemptRow | None = None,
    transcript: TranscriptRow | None = None,
    source_turn: TurnRow | None = None,
) -> dict[str, object]:
    return {
        "id": str(row.id),
        "status": row.status,
        "kind": row.kind,
        "content": row.content,
        "scope": row.scope,
        "scope_key": row.scope_key,
        "confidence": row.confidence,
        "durability": row.durability,
        "proposed_action": row.proposed_action,
        "subject": row.subject,
        "namespace": row.namespace,
        "evidence": row.evidence,
        "attempt_count": int(row.attempt_count or 0),
        "decision": row.decision_json or {},
        "review_after": row.review_after,
        "expires_at": row.expires_at,
        "invalidated_at": row.invalidated_at,
        "created_at": row.created_at,
        "processed_at": row.processed_at,
        "source_transcript_id": str(row.source_transcript_id),
        "source_turn_id": str(row.source_turn_id),
        "source_provider_evidence_id": (
            str(row.source_provider_evidence_id) if row.source_provider_evidence_id else None
        ),
        "source_chat_title": transcript.title if transcript else None,
        "source_sequence": int(source_turn.sequence) if source_turn else None,
        "latest_attempt": _attempt_projection(latest_attempt) if latest_attempt else None,
    }


def _memory_projection(row: DurableMemoryRow) -> dict[str, object]:
    return {
        "id": str(row.id),
        "status": row.status,
        "authority": "owner" if row.record_kind.startswith("owner_") else "derived",
        "record_kind": row.record_kind,
        "memory_kind": row.memory_kind,
        "scope": row.scope,
        "scope_key": row.scope_key,
        "durability": row.durability,
        "subject": row.subject,
        "namespace": row.namespace,
        "content": row.content,
        "suppresses_recall": row.suppresses_recall,
        "embedded": row.embedding is not None,
        "embedding_model": row.embedding_model,
        "source_transcript_id": str(row.source_transcript_id) if row.source_transcript_id else None,
        "source_turn_id": str(row.source_turn_id) if row.source_turn_id else None,
        "supersedes_id": str(row.supersedes_id) if row.supersedes_id else None,
        "superseded_by_id": str(row.superseded_by_id) if row.superseded_by_id else None,
        "valid_from": row.valid_from,
        "valid_to": row.valid_to,
        "retired_at": row.retired_at,
        "deleted_at": row.deleted_at,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


class MemoryObservabilityService:
    """Read-only projections for owner inspection of the memory pipeline."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def change_token(self) -> str:
        """Compact fingerprint of observable memory state for live Control streaming."""
        candidate_state = [
            [str(status), int(count), str(created or ""), str(processed or ""), str(leased or ""), str(review or "")]
            for status, count, created, processed, leased, review in (
                await self.session.execute(
                    select(
                        MemoryCandidateRow.status,
                        func.count(),
                        func.max(MemoryCandidateRow.created_at),
                        func.max(MemoryCandidateRow.processed_at),
                        func.max(MemoryCandidateRow.leased_until),
                        func.max(MemoryCandidateRow.review_after),
                    )
                    .group_by(MemoryCandidateRow.status)
                    .order_by(MemoryCandidateRow.status)
                )
            ).all()
        ]
        attempt_state = [
            [str(status), int(count), str(started or ""), str(completed or "")]
            for status, count, started, completed in (
                await self.session.execute(
                    select(
                        MemoryReconciliationAttemptRow.status,
                        func.count(),
                        func.max(MemoryReconciliationAttemptRow.started_at),
                        func.max(MemoryReconciliationAttemptRow.completed_at),
                    )
                    .group_by(MemoryReconciliationAttemptRow.status)
                    .order_by(MemoryReconciliationAttemptRow.status)
                )
            ).all()
        ]
        memory_state = [
            [str(status), str(kind), int(count), str(updated or ""), str(embedded or "")]
            for status, kind, count, updated, embedded in (
                await self.session.execute(
                    select(
                        DurableMemoryRow.status,
                        DurableMemoryRow.record_kind,
                        func.count(),
                        func.max(DurableMemoryRow.updated_at),
                        func.max(DurableMemoryRow.embedded_at),
                    )
                    .group_by(DurableMemoryRow.status, DurableMemoryRow.record_kind)
                    .order_by(DurableMemoryRow.status, DurableMemoryRow.record_kind)
                )
            ).all()
        ]
        payload = json.dumps(
            {"candidates": candidate_state, "attempts": attempt_state, "memories": memory_state},
            sort_keys=True, separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    async def overview(self, *, limit: int = 40) -> dict[str, object]:
        limit = max(1, min(int(limit), 100))
        candidate_counts = {
            str(status): int(count)
            for status, count in (
                await self.session.execute(
                    select(MemoryCandidateRow.status, func.count())
                    .group_by(MemoryCandidateRow.status)
                    .order_by(MemoryCandidateRow.status)
                )
            ).all()
        }
        memory_counts = {
            str(status): int(count)
            for status, count in (
                await self.session.execute(
                    select(DurableMemoryRow.status, func.count())
                    .group_by(DurableMemoryRow.status)
                    .order_by(DurableMemoryRow.status)
                )
            ).all()
        }
        authority_counts = {
            "owner": int(
                (
                    await self.session.execute(
                        select(func.count())
                        .select_from(DurableMemoryRow)
                        .where(DurableMemoryRow.record_kind.like("owner_%"))
                    )
                ).scalar_one()
            ),
            "derived": int(
                (
                    await self.session.execute(
                        select(func.count())
                        .select_from(DurableMemoryRow)
                        .where(~DurableMemoryRow.record_kind.like("owner_%"))
                    )
                ).scalar_one()
            ),
        }

        candidates = list(
            (
                await self.session.execute(
                    select(MemoryCandidateRow)
                    .order_by(MemoryCandidateRow.created_at.desc(), MemoryCandidateRow.id.desc())
                    .limit(limit)
                )
            ).scalars()
        )
        candidate_ids = [row.id for row in candidates]
        transcript_ids = {row.source_transcript_id for row in candidates}
        source_turn_ids = {row.source_turn_id for row in candidates}

        latest_attempts: dict[UUID, MemoryReconciliationAttemptRow] = {}
        if candidate_ids:
            attempts = list(
                (
                    await self.session.execute(
                        select(MemoryReconciliationAttemptRow)
                        .where(MemoryReconciliationAttemptRow.candidate_id.in_(candidate_ids))
                        .order_by(
                            MemoryReconciliationAttemptRow.candidate_id,
                            MemoryReconciliationAttemptRow.attempt_number.desc(),
                        )
                    )
                ).scalars()
            )
            for attempt in attempts:
                latest_attempts.setdefault(attempt.candidate_id, attempt)

        transcripts: dict[UUID, TranscriptRow] = {}
        if transcript_ids:
            rows = list(
                (
                    await self.session.execute(
                        select(TranscriptRow).where(TranscriptRow.id.in_(transcript_ids))
                    )
                ).scalars()
            )
            transcripts = {row.id: row for row in rows}

        source_turns: dict[UUID, TurnRow] = {}
        if source_turn_ids:
            rows = list(
                (
                    await self.session.execute(
                        select(TurnRow).where(TurnRow.id.in_(source_turn_ids))
                    )
                ).scalars()
            )
            source_turns = {row.id: row for row in rows}

        memories = list(
            (
                await self.session.execute(
                    select(DurableMemoryRow)
                    .order_by(DurableMemoryRow.updated_at.desc(), DurableMemoryRow.id.desc())
                    .limit(limit)
                )
            ).scalars()
        )
        latest_attempt = (
            await self.session.execute(
                select(MemoryReconciliationAttemptRow)
                .order_by(MemoryReconciliationAttemptRow.started_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

        return {
            "summary": {
                "candidate_counts": candidate_counts,
                "memory_counts": memory_counts,
                "authority_counts": authority_counts,
                "last_attempt": _attempt_projection(latest_attempt) if latest_attempt else None,
            },
            "recent_candidates": [
                _candidate_projection(
                    row,
                    latest_attempt=latest_attempts.get(row.id),
                    transcript=transcripts.get(row.source_transcript_id),
                    source_turn=source_turns.get(row.source_turn_id),
                )
                for row in candidates
            ],
            "recent_memories": [_memory_projection(row) for row in memories],
        }

    async def candidate_detail(self, candidate_id: UUID) -> dict[str, object]:
        candidate = await self.session.get(MemoryCandidateRow, candidate_id)
        if candidate is None:
            raise LookupError("Memory candidate not found")

        transcript = await self.session.get(TranscriptRow, candidate.source_transcript_id)
        source_turn = await self.session.get(TurnRow, candidate.source_turn_id)
        attempts = list(
            (
                await self.session.execute(
                    select(MemoryReconciliationAttemptRow)
                    .where(MemoryReconciliationAttemptRow.candidate_id == candidate.id)
                    .order_by(MemoryReconciliationAttemptRow.attempt_number.desc())
                    .limit(20)
                )
            ).scalars()
        )
        provenance = list(
            (
                await self.session.execute(
                    select(MemoryProvenanceRow)
                    .where(MemoryProvenanceRow.source_candidate_id == candidate.id)
                    .order_by(MemoryProvenanceRow.created_at.desc())
                    .limit(50)
                )
            ).scalars()
        )
        linked_memory_ids = {row.memory_id for row in provenance}
        linked_memory_ids.update(
            row.target_memory_id for row in attempts if row.target_memory_id is not None
        )
        linked_memories: list[DurableMemoryRow] = []
        if linked_memory_ids:
            linked_memories = list(
                (
                    await self.session.execute(
                        select(DurableMemoryRow)
                        .where(DurableMemoryRow.id.in_(linked_memory_ids))
                        .order_by(DurableMemoryRow.updated_at.desc())
                    )
                ).scalars()
            )

        provenance_by_memory: dict[UUID, list[dict[str, object]]] = defaultdict(list)
        for row in provenance:
            provenance_by_memory[row.memory_id].append(
                {
                    "id": str(row.id),
                    "relationship": row.relationship,
                    "source_turn_id": str(row.source_turn_id) if row.source_turn_id else None,
                    "source_memory_id": str(row.source_memory_id) if row.source_memory_id else None,
                    "created_at": row.created_at,
                }
            )

        latest_attempt = attempts[0] if attempts else None
        return {
            "candidate": _candidate_projection(
                candidate,
                latest_attempt=latest_attempt,
                transcript=transcript,
                source_turn=source_turn,
            ),
            "source": {
                "chat_title": transcript.title if transcript else None,
                "turn_id": str(source_turn.id) if source_turn else str(candidate.source_turn_id),
                "sequence": int(source_turn.sequence) if source_turn else None,
                "actor": source_turn.actor if source_turn else None,
                "deleted": bool(source_turn and source_turn.deleted_at is not None),
                "text": _turn_text(source_turn),
            },
            "attempts": [_attempt_projection(row) for row in attempts],
            "linked_memories": [
                {
                    **_memory_projection(row),
                    "provenance": provenance_by_memory.get(row.id, []),
                }
                for row in linked_memories
            ],
            "reason_retained": False,
        }
