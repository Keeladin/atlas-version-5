from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from atlas.persistence.models import (
    DurableMemoryRow,
    MemoryCandidateEvidenceRow,
    MemoryCandidateRow,
    MemoryComparisonVerdictRow,
    MemoryConflictRow,
    MemoryIndependentReadingRow,
    MemoryObligationRow,
    MemoryPolicyDecisionRow,
    MemoryProvenanceRow,
    MemoryReconciliationAttemptRow,
    MemoryReconciliationRecordRow,
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
        "proposer_model": row.proposer_model,
        "intake_path": row.intake_path,
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
        "grounding_status": row.grounding_status,
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
            [
                str(status), str(kind), str(grounding), int(count),
                str(updated or ""), str(embedded or ""),
            ]
            for status, kind, grounding, count, updated, embedded in (
                await self.session.execute(
                    select(
                        DurableMemoryRow.status,
                        DurableMemoryRow.record_kind,
                        DurableMemoryRow.grounding_status,
                        func.count(),
                        func.max(DurableMemoryRow.updated_at),
                        func.max(DurableMemoryRow.embedded_at),
                    )
                    .group_by(
                        DurableMemoryRow.status,
                        DurableMemoryRow.record_kind,
                        DurableMemoryRow.grounding_status,
                    )
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
        grounding_counts = {
            str(status): int(count)
            for status, count in (
                await self.session.execute(
                    select(DurableMemoryRow.grounding_status, func.count())
                    .group_by(DurableMemoryRow.grounding_status)
                    .order_by(DurableMemoryRow.grounding_status)
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
                "grounding_counts": grounding_counts,
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
        trigger_turn = await self.session.get(TurnRow, candidate.source_turn_id)
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
                    .limit(100)
                )
            ).scalars()
        )

        evidence_refs = list(
            (
                await self.session.execute(
                    select(MemoryCandidateEvidenceRow)
                    .where(MemoryCandidateEvidenceRow.candidate_id == candidate.id)
                    .order_by(MemoryCandidateEvidenceRow.ordinal.asc())
                )
            ).scalars()
        )
        evidence_turn_ids = {row.turn_id for row in evidence_refs}
        evidence_turns: dict[UUID, TurnRow] = {}
        if evidence_turn_ids:
            rows = list(
                (
                    await self.session.execute(
                        select(TurnRow).where(TurnRow.id.in_(evidence_turn_ids))
                    )
                ).scalars()
            )
            evidence_turns = {row.id: row for row in rows}

        readings = list(
            (
                await self.session.execute(
                    select(MemoryIndependentReadingRow)
                    .where(MemoryIndependentReadingRow.candidate_id == candidate.id)
                    .order_by(MemoryIndependentReadingRow.created_at.desc())
                )
            ).scalars()
        )
        comparisons = list(
            (
                await self.session.execute(
                    select(MemoryComparisonVerdictRow)
                    .where(MemoryComparisonVerdictRow.candidate_id == candidate.id)
                    .order_by(MemoryComparisonVerdictRow.created_at.desc())
                )
            ).scalars()
        )
        reconciliations = list(
            (
                await self.session.execute(
                    select(MemoryReconciliationRecordRow)
                    .where(MemoryReconciliationRecordRow.candidate_id == candidate.id)
                    .order_by(MemoryReconciliationRecordRow.created_at.desc())
                )
            ).scalars()
        )
        policies = list(
            (
                await self.session.execute(
                    select(MemoryPolicyDecisionRow)
                    .where(MemoryPolicyDecisionRow.candidate_id == candidate.id)
                    .order_by(MemoryPolicyDecisionRow.created_at.desc())
                )
            ).scalars()
        )
        conflicts = list(
            (
                await self.session.execute(
                    select(MemoryConflictRow)
                    .where(MemoryConflictRow.candidate_id == candidate.id)
                    .order_by(MemoryConflictRow.created_at.desc())
                )
            ).scalars()
        )
        conflict_ids = {row.id for row in conflicts}
        obligation_conditions = [
            (MemoryObligationRow.subject_type == "memory_candidate")
            & (MemoryObligationRow.subject_id == candidate.id)
        ]
        if conflict_ids:
            obligation_conditions.append(
                (MemoryObligationRow.subject_type == "memory_conflict")
                & MemoryObligationRow.subject_id.in_(conflict_ids)
            )
        obligations = list(
            (
                await self.session.execute(
                    select(MemoryObligationRow)
                    .where(*[obligation_conditions[0] | item for item in obligation_conditions[1:]])
                    .order_by(MemoryObligationRow.created_at.desc())
                )
            ).scalars()
        ) if len(obligation_conditions) > 1 else list(
            (
                await self.session.execute(
                    select(MemoryObligationRow)
                    .where(obligation_conditions[0])
                    .order_by(MemoryObligationRow.created_at.desc())
                )
            ).scalars()
        )

        linked_memory_ids = {row.memory_id for row in provenance}
        linked_memory_ids.update(
            row.target_memory_id for row in attempts if row.target_memory_id is not None
        )
        linked_memory_ids.update(
            row.target_memory_id for row in conflicts if row.target_memory_id is not None
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

        trigger = {
            "chat_title": transcript.title if transcript else None,
            "turn_id": str(trigger_turn.id) if trigger_turn else str(candidate.source_turn_id),
            "sequence": int(trigger_turn.sequence) if trigger_turn else None,
            "actor": trigger_turn.actor if trigger_turn else None,
            "deleted": bool(trigger_turn and trigger_turn.deleted_at is not None),
            "text": _turn_text(trigger_turn),
        }
        evidence_sources = []
        for ref in evidence_refs:
            turn = evidence_turns.get(ref.turn_id)
            evidence_sources.append({
                "turn_id": str(ref.turn_id),
                "span_ref": ref.span_ref,
                "principal": ref.principal,
                "sequence": int(turn.sequence) if turn else None,
                "actor": turn.actor if turn else None,
                "deleted": bool(turn and turn.deleted_at is not None),
                "text": _turn_text(turn),
            })

        latest_attempt = attempts[0] if attempts else None
        return {
            "candidate": _candidate_projection(
                candidate,
                latest_attempt=latest_attempt,
                transcript=transcript,
                source_turn=trigger_turn,
            ),
            "trigger": trigger,
            # Backward-compatible alias for clients built before trigger/evidence separation.
            "source": trigger,
            "evidence_sources": evidence_sources,
            "verification": {
                "independent_readings": [
                    {
                        "id": str(row.id),
                        "attempt_id": str(row.attempt_id),
                        "evidence_set_hash": row.evidence_set_hash,
                        "source_revision": int(row.source_revision),
                        "extracted_claims": list(row.extracted_claims_json or []),
                        "category": row.category,
                        "scope": row.scope,
                        "durability": row.durability,
                        "event_valid_from": row.event_valid_from,
                        "event_valid_to": row.event_valid_to,
                        "verifier_model": row.verifier_model,
                        "tombstoned": row.tombstoned_at is not None,
                        "created_at": row.created_at,
                    }
                    for row in readings
                ],
                "comparisons": [
                    {
                        "id": str(row.id),
                        "reading_id": str(row.reading_id),
                        "verdict": row.verdict,
                        "normalized_content": row.normalized_content,
                        "category": row.category,
                        "scope": row.scope,
                        "durability": row.durability,
                        "tombstoned": row.tombstoned_at is not None,
                        "created_at": row.created_at,
                    }
                    for row in comparisons
                ],
                "reconciliations": [
                    {
                        "id": str(row.id),
                        "comparison_id": str(row.comparison_id),
                        "relation": row.relation,
                        "target_memory_id": (
                            str(row.target_memory_id) if row.target_memory_id else None
                        ),
                        "evaluated_memory_revision": int(row.evaluated_memory_revision),
                        "replacement_content": row.replacement_content,
                        "temporal_guard": row.temporal_guard,
                        "tombstoned": row.tombstoned_at is not None,
                        "created_at": row.created_at,
                    }
                    for row in reconciliations
                ],
                "policies": [
                    {
                        "id": str(row.id),
                        "reconciliation_id": str(row.reconciliation_id),
                        "decision": row.decision,
                        "reason_code": row.reason_code,
                        "created_at": row.created_at,
                    }
                    for row in policies
                ],
                "obligations": [
                    {
                        "id": str(row.id),
                        "kind": row.kind,
                        "status": row.status,
                        "subject_type": row.subject_type,
                        "subject_id": str(row.subject_id) if row.subject_id else None,
                        "resolution_code": row.resolution_code,
                        "expires_at": row.expires_at,
                        "created_at": row.created_at,
                        "resolved_at": row.resolved_at,
                    }
                    for row in obligations
                ],
                "conflicts": [
                    {
                        "id": str(row.id),
                        "status": row.status,
                        "target_memory_id": str(row.target_memory_id),
                        "proposed_content": row.proposed_content,
                        "reason_code": row.reason_code,
                        "tombstoned": row.tombstoned_at is not None,
                        "created_at": row.created_at,
                        "resolved_at": row.resolved_at,
                    }
                    for row in conflicts
                ],
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
