
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import ClassVar, Literal, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from atlas.persistence.models import (
    DurableMemoryRow,
    MemoryCandidateEvidenceRow,
    MemoryCandidateRow,
    MemoryCommandRow,
    MemoryComparisonVerdictRow,
    MemoryConflictRow,
    MemoryIndependentReadingRow,
    MemoryObligationRow,
    MemoryPolicyDecisionRow,
    MemoryProvenanceRow,
    MemoryReconciliationAttemptRow,
    MemoryReconciliationRecordRow,
    SharedResourceVersionRow,
    TranscriptRow,
    TurnRow,
)
from atlas.persistence.shared_writes import (
    SharedStateWriter,
    SharedWriteEnvelope,
    SharedWriteMutation,
    SharedWriteReceipt,
)

from .candidates import protocol_token
from .durable import (
    ACTIVE,
    RETIRED,
    SUPERSEDED,
    DurableMemoryRepository,
    clean_memory_content,
    memory_fingerprint,
    utcnow,
)
from .explicit import EXPLICIT_OBLIGATION_KINDS, settle_explicit_obligation
from .state_machine import (
    PublicationBucket,
    PublicationOutcome,
    PublicationTerminality,
)

logger = logging.getLogger(__name__)


def _evidence_turn_admissible(candidate: MemoryCandidateRow, turn: TurnRow) -> bool:
    """Candidate evidence must come from its own transcript.

    The one exception is the owner assertion appended by a confirmation: it is
    written into a dependency-protected review transcript and recorded on the
    candidate, so it is admissible even though it lives elsewhere.
    """
    if turn.deleted_at is not None:
        return False
    if turn.transcript_id == candidate.source_transcript_id:
        return True
    assertion = (candidate.decision_json or {}).get("owner_assertion_turn_id")
    return bool(assertion) and str(turn.id) == str(assertion) and turn.actor == "owner"

_MEMORY_RESOURCE_TYPE = "memory_state"
_MEMORY_RESOURCE_ID = "owner"


def _is_retryable_serialization_error(exc: BaseException) -> bool:
    current: BaseException | None = exc
    for _ in range(4):
        code = getattr(current, "sqlstate", None) or getattr(current, "pgcode", None)
        if code in {"40001", "40P01"}:
            return True
        current = getattr(current, "orig", None) or getattr(current, "__cause__", None)
        if current is None:
            break
    return False

_TERMINAL_CANDIDATE_STATUSES = {
    "discarded",
    "reconciled",
    "blocked",
    "expired",
    "failed",
    "invalidated",
}


class ReconciliationModel(Protocol):
    async def complete_text(
        self, *, instructions: str, messages: list[dict[str, str]]
    ) -> str: ...


class ReconciliationDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal[
        "discard",
        "retain_short_term",
        "create",
        "equivalent",
        "merge",
        "supersede",
        "historical_predecessor",
        "ground_legacy",
        "conflict",
        "await_owner",
    ]
    target_memory_id: UUID | None = None
    content: str | None = Field(default=None, max_length=4_000)
    reason: str | None = Field(default=None, max_length=500)
    event_valid_from: datetime | None = None
    event_valid_to: datetime | None = None
    memory_kind: str | None = Field(default=None, max_length=64)
    scope: Literal["chat", "project", "cross_chat"] | None = None
    durability: Literal["short_term", "long_term"] | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> ReconciliationDecision:
        targeted = {
            "equivalent", "merge", "supersede", "historical_predecessor", "ground_legacy", "conflict"
        }
        if self.decision in targeted and self.target_memory_id is None:
            raise ValueError(f"{self.decision} requires target_memory_id")
        if self.decision not in targeted and self.target_memory_id is not None:
            raise ValueError(f"{self.decision} must not select target_memory_id")
        if self.decision in {"merge", "supersede", "historical_predecessor", "ground_legacy"} and not (self.content or "").strip():
            raise ValueError(f"{self.decision} requires replacement content")
        return self


class IndependentReading(BaseModel):
    model_config = ConfigDict(extra="forbid")

    extracted_claims: list[str] = Field(default_factory=list, max_length=8)
    category: str | None = Field(default=None, max_length=64)
    scope: Literal["chat", "project", "cross_chat"] | None = None
    durability: Literal["short_term", "long_term"] | None = None
    claim_principal: Literal["owner", "external", "mixed", "unknown"] | None = None
    event_valid_from: datetime | None = None
    event_valid_to: datetime | None = None


_READING_TOKENS: dict[str, frozenset[str]] = {
    "scope": frozenset({"chat", "project", "cross_chat"}),
    "durability": frozenset({"short_term", "long_term"}),
    "claim_principal": frozenset({"owner", "external", "mixed", "unknown"}),
}
_MAX_READING_CLAIMS = 8


def _fold_optional_token(
    data: dict[str, object], key: str, allowed: frozenset[str], notes: dict[str, int]
) -> None:
    """Syntax-only folding of an optional classification hint.

    A spelling variant of an allowed value is folded; ``global`` means ``cross_chat``.
    Any other value is dropped to null and counted, so a bad hint never sinks the
    stage output it decorates. Meaning is never reinterpreted.
    """
    value = data.get(key)
    if value is None:
        return
    if not isinstance(value, str):
        data[key] = None
        notes[f"invalid_{key}"] = notes.get(f"invalid_{key}", 0) + 1
        return
    token = protocol_token(value)
    if key == "scope" and token == "global":
        token = "cross_chat"
    if token in allowed:
        data[key] = token
    elif token in {"", "null", "none"}:
        data[key] = None
    else:
        data[key] = None
        notes[f"invalid_{key}"] = notes.get(f"invalid_{key}", 0) + 1


def _fold_required_token(data: dict[str, object], key: str) -> None:
    """Spelling variants of a required protocol token fold; unknown values stay and fail."""
    value = data.get(key)
    if isinstance(value, str):
        data[key] = protocol_token(value)


def normalize_reading_payload(raw: object) -> tuple[object, dict[str, int]]:
    notes: dict[str, int] = {}
    if not isinstance(raw, dict):
        return raw, notes
    data = dict(raw)
    claims = data.get("extracted_claims")
    if isinstance(claims, list):
        kept = [item for item in claims if isinstance(item, str) and item.strip()]
        dropped = len(claims) - len(kept)
        if dropped:
            notes["claim_dropped"] = dropped
        if len(kept) > _MAX_READING_CLAIMS:
            # Over-extraction: the retained claims are still individually supported;
            # the comparison stage judges the proposal against what remains.
            notes["claims_truncated"] = len(kept) - _MAX_READING_CLAIMS
            kept = kept[:_MAX_READING_CLAIMS]
        data["extracted_claims"] = kept
    for key, allowed in _READING_TOKENS.items():
        _fold_optional_token(data, key, allowed, notes)
    return data, notes


def normalize_comparison_payload(raw: object) -> tuple[object, dict[str, int]]:
    notes: dict[str, int] = {}
    if not isinstance(raw, dict):
        return raw, notes
    data = dict(raw)
    _fold_required_token(data, "verdict")
    for key in ("scope", "durability"):
        _fold_optional_token(data, key, _READING_TOKENS[key], notes)
    return data, notes


def normalize_relation_payload(raw: object) -> tuple[object, dict[str, int]]:
    if not isinstance(raw, dict):
        return raw, {}
    data = dict(raw)
    _fold_required_token(data, "relation")
    return data, {}


class ComparisonVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: Literal[
        "agree",
        "narrow_scope",
        "contradicted",
        "insufficient_evidence",
        "different_category",
    ]
    normalized_content: str | None = Field(default=None, max_length=4_000)
    category: str | None = Field(default=None, max_length=64)
    scope: Literal["chat", "project", "cross_chat"] | None = None
    durability: Literal["short_term", "long_term"] | None = None


class MemoryRelationDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    relation: Literal[
        "new",
        "duplicate_of",
        "narrows",
        "supersedes",
        "historical_predecessor",
        "grounds_legacy",
        "conflicts_with",
    ]
    target_memory_id: UUID | None = None
    replacement_content: str | None = Field(default=None, max_length=4_000)
    temporal_guard: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def validate_shape(self) -> MemoryRelationDecision:
        targeted = {
            "duplicate_of",
            "narrows",
            "supersedes",
            "historical_predecessor",
            "grounds_legacy",
            "conflicts_with",
        }
        if self.relation in targeted and self.target_memory_id is None:
            raise ValueError(f"{self.relation} requires target_memory_id")
        if self.relation == "new" and self.target_memory_id is not None:
            raise ValueError("new must not select target_memory_id")
        return self


@dataclass(frozen=True)
class CandidateLease:
    candidate_id: UUID
    attempt_id: UUID
    lease_token: UUID
    attempt_number: int


@dataclass(frozen=True)
class EvaluationSnapshot:
    candidate_id: UUID
    source_transcript_id: UUID
    source_turn_id: UUID
    source_provider_evidence_id: UUID | None
    source_revision: int
    memory_revision: int
    candidate: dict[str, object]
    source_text: str
    evidence_items: list[dict[str, object]]
    active_memories: list[dict[str, object]]
    restricted_memories: list[dict[str, object]]
    legacy_unverified_targets: list[dict[str, object]]
    evidence_refs: dict[str, object]
    publication_allowed: bool


@dataclass(frozen=True)
class ReconciliationRunResult:
    claimed: int = 0
    reconciled: int = 0
    retained_short_term: int = 0
    discarded: int = 0
    blocked: int = 0
    version_conflicts: int = 0
    source_conflicts: int = 0
    lease_conflicts: int = 0
    failures: int = 0
    expired: int = 0
    contract_notes: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "reconciliation_contract_notes": dict(self.contract_notes),
            "reconciliation_claimed": self.claimed,
            "reconciliation_reconciled": self.reconciled,
            "reconciliation_retained_short_term": self.retained_short_term,
            "reconciliation_discarded": self.discarded,
            "reconciliation_blocked": self.blocked,
            "reconciliation_version_conflicts": self.version_conflicts,
            "reconciliation_source_conflicts": self.source_conflicts,
            "reconciliation_lease_conflicts": self.lease_conflicts,
            "reconciliation_failures": self.failures,
            "reconciliation_expired": self.expired,
        }


class CandidateSnapshotError(RuntimeError):
    pass


def _clip(value: object, limit: int) -> str:
    return " ".join(str(value or "").split()).strip()[:limit]


def _owner_directed(row: DurableMemoryRow) -> bool:
    return row.record_kind == "owner_directed" or row.record_kind.startswith("owner_")


def _advance_candidate_state(
    candidate: MemoryCandidateRow, attempt: MemoryReconciliationAttemptRow
) -> None:
    current = int(candidate.state_version or 0)
    expected = int(attempt.candidate_state_version or 0)
    if current != expected:
        raise CandidateSnapshotError("candidate state changed during worker advance")
    next_version = current + 1
    candidate.state_version = next_version
    attempt.candidate_state_version = next_version


def _clear_embedding(row: DurableMemoryRow) -> None:
    row.embedding = None
    row.embedding_model = None
    row.embedding_dimensions = None
    row.embedded_at = None


def _source_text(row: TurnRow, limit: int = 4_000) -> str:
    parts: list[str] = []
    for block in row.blocks or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            value = _clip(block.get("text"), limit)
            if value:
                parts.append(value)
        elif block.get("type") == "tool_observation":
            operation = _clip(block.get("operation") or "runtime", 128)
            phase = _clip(block.get("phase") or "observed", 64)
            summary = _clip(block.get("summary"), 500)
            parts.append(f"{operation} [{phase}]" + (f": {summary}" if summary else ""))
    return _clip(" | ".join(parts), limit)


def _project_for_model(row: DurableMemoryRow) -> dict[str, object]:
    return {
        "memory_id": str(row.id),
        "authority": "owner_directed" if _owner_directed(row) else "derived",
        "status": row.status,
        "grounding_status": row.grounding_status,
        "origin": row.origin,
        "kind": row.memory_kind,
        "scope": row.scope,
        "scope_key": row.scope_key,
        "durability": row.durability,
        "subject": row.subject,
        "namespace": row.namespace,
        "content": _clip(row.content, 1_200),
        "event_valid_from": row.valid_from,
        "event_valid_to": row.valid_to,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _scope_condition(transcript_id: UUID):
    return or_(
        DurableMemoryRow.scope == "cross_chat",
        and_(
            DurableMemoryRow.scope == "chat",
            DurableMemoryRow.scope_key == f"chat:{transcript_id}",
        ),
    )


async def _memory_revision(session: AsyncSession) -> int:
    row = await session.get(
        SharedResourceVersionRow,
        {"resource_type": _MEMORY_RESOURCE_TYPE, "resource_id": _MEMORY_RESOURCE_ID},
    )
    return int(row.version or 0) if row is not None else 0


class MemoryCandidateLeaseRepository:
    def __init__(
        self,
        factory: async_sessionmaker,
        *,
        lease_seconds: int = 180,
        max_attempts: int = 3,
    ) -> None:
        self.factory = factory
        self.lease_seconds = max(30, min(int(lease_seconds), 1_800))
        self.max_attempts = max(1, min(int(max_attempts), 10))

    async def claim_batch(self, *, limit: int = 8) -> tuple[list[CandidateLease], int]:
        now = datetime.now(UTC)
        bounded = max(1, min(int(limit), 32))
        claimed: list[CandidateLease] = []
        async with self.factory() as session, session.begin():
            expired_obligations = await self._expire_due_obligations(session, now)
            expired_rows = list(
                (
                    await session.execute(
                        select(MemoryCandidateRow)
                        .where(
                            MemoryCandidateRow.status == "retained_short_term",
                            MemoryCandidateRow.expires_at.is_not(None),
                            MemoryCandidateRow.expires_at <= now,
                        )
                        .with_for_update(skip_locked=True)
                    )
                ).scalars()
            )
            for row in expired_rows:
                row.state_version = int(row.state_version or 0) + 1
                row.status = "expired"
                row.processed_at = now
                row.lease_token = None
                row.leased_until = None
                row.review_after = None
                row.decision_json = {"decision": "expired"}
                await settle_explicit_obligation(
                    session, row, now=now, code="retention_expired"
                )

            exhausted = list(
                (
                    await session.execute(
                        select(MemoryCandidateRow)
                        .where(
                            or_(
                                and_(
                                    MemoryCandidateRow.status == "leased",
                                    MemoryCandidateRow.leased_until.is_not(None),
                                    MemoryCandidateRow.leased_until <= now,
                                ),
                                MemoryCandidateRow.status == "pending",
                            ),
                            MemoryCandidateRow.attempt_count >= self.max_attempts,
                        )
                        .with_for_update(skip_locked=True)
                    )
                ).scalars()
            )
            for row in exhausted:
                row.state_version = int(row.state_version or 0) + 1
                await self._close_stale_attempts(
                    session, row.id, now, status="attempt_limit"
                )
                row.status = "failed"
                row.lease_token = None
                row.leased_until = None
                row.processed_at = now
                row.decision_json = {
                    "decision": "failed",
                    "code": "attempt_limit",
                }
                await settle_explicit_obligation(
                    session, row, now=now, code="attempt_limit"
                )

            eligible = or_(
                and_(
                    MemoryCandidateRow.status == "pending",
                    MemoryCandidateRow.attempt_count < self.max_attempts,
                ),
                and_(
                    MemoryCandidateRow.status == "leased",
                    MemoryCandidateRow.leased_until.is_not(None),
                    MemoryCandidateRow.leased_until <= now,
                    MemoryCandidateRow.attempt_count < self.max_attempts,
                ),
                and_(
                    MemoryCandidateRow.status == "retained_short_term",
                    MemoryCandidateRow.review_after.is_not(None),
                    MemoryCandidateRow.review_after <= now,
                    or_(
                        MemoryCandidateRow.expires_at.is_(None),
                        MemoryCandidateRow.expires_at > now,
                    ),
                ),
            )
            rows = list(
                (
                    await session.execute(
                        select(MemoryCandidateRow)
                        .where(
                            eligible,
                            MemoryCandidateRow.invalidated_at.is_(None),
                            MemoryCandidateRow.content.is_not(None),
                            MemoryCandidateRow.fingerprint.is_not(None),
                        )
                        .order_by(
                            MemoryCandidateRow.created_at.asc(),
                            MemoryCandidateRow.id.asc(),
                        )
                        .limit(bounded)
                        .with_for_update(skip_locked=True)
                    )
                ).scalars()
            )
            for row in rows:
                if not await self._evidence_live(session, row):
                    await self._close_stale_attempts(
                        session, row.id, now, status="evidence_unavailable"
                    )
                    row.state_version = int(row.state_version or 0) + 1
                    row.status = "invalidated"
                    row.invalidated_at = now
                    row.lease_token = None
                    row.leased_until = None
                    row.review_after = None
                    row.processed_at = now
                    row.decision_json = {
                        "decision": "invalidated",
                        "code": "evidence_unavailable",
                    }
                    await settle_explicit_obligation(
                        session, row, now=now, code="evidence_unavailable"
                    )
                    continue
                if row.status == "leased":
                    await self._close_stale_attempts(
                        session, row.id, now, status="lease_expired"
                    )
                attempt_number = int(
                    (
                        await session.execute(
                            select(
                                func.coalesce(
                                    func.max(
                                        MemoryReconciliationAttemptRow.attempt_number
                                    ),
                                    0,
                                )
                            ).where(
                                MemoryReconciliationAttemptRow.candidate_id == row.id
                            )
                        )
                    ).scalar_one()
                ) + 1
                token = uuid4()
                row.state_version = int(row.state_version or 0) + 1
                attempt = MemoryReconciliationAttemptRow(
                    candidate_id=row.id,
                    lease_token=token,
                    attempt_number=attempt_number,
                    candidate_state_version=row.state_version,
                    status="claimed",
                    evidence_json={},
                    result_json={},
                )
                session.add(attempt)
                row.status = "leased"
                row.lease_token = token
                row.leased_until = now + timedelta(seconds=self.lease_seconds)
                row.attempt_count = int(row.attempt_count or 0) + 1
                row.review_after = None
                await session.flush()
                claimed.append(
                    CandidateLease(
                        candidate_id=row.id,
                        attempt_id=attempt.id,
                        lease_token=token,
                        attempt_number=attempt_number,
                    )
                )
        return claimed, len(expired_rows) + expired_obligations

    @staticmethod
    async def _expire_due_obligations(
        session: AsyncSession, now: datetime
    ) -> int:
        obligations = list(
            (
                await session.execute(
                    select(MemoryObligationRow)
                    .where(
                        MemoryObligationRow.status == "pending",
                        MemoryObligationRow.expires_at.is_not(None),
                        MemoryObligationRow.expires_at <= now,
                    )
                    .with_for_update(skip_locked=True)
                )
            ).scalars()
        )
        for obligation in obligations:
            obligation.status = "resolved"
            obligation.resolution_code = "expired_unconfirmed"
            obligation.resolution_json = {}
            obligation.resolved_at = now
            if obligation.subject_type == "memory_candidate" and obligation.subject_id:
                candidate = await session.get(MemoryCandidateRow, obligation.subject_id)
                if candidate is not None and candidate.status == "awaiting_owner":
                    candidate.state_version = int(candidate.state_version or 0) + 1
                    candidate.status = "expired"
                    candidate.lease_token = None
                    candidate.leased_until = None
                    candidate.review_after = None
                    candidate.processed_at = now
                    candidate.decision_json = {
                        "decision": "expired",
                        "code": "owner_confirmation_expired",
                    }
                    await settle_explicit_obligation(
                        session, candidate, now=now, code="owner_confirmation_expired"
                    )
            elif obligation.subject_type == "memory_conflict" and obligation.subject_id:
                conflict = await session.get(MemoryConflictRow, obligation.subject_id)
                if conflict is not None and conflict.status == "open":
                    conflict.status = "expired_unresolved"
                    conflict.resolved_at = now
                    candidate = await session.get(MemoryCandidateRow, conflict.candidate_id)
                    if candidate is not None and candidate.status == "awaiting_owner":
                        candidate.state_version = int(candidate.state_version or 0) + 1
                        candidate.status = "expired"
                        candidate.processed_at = now
                        candidate.decision_json = {
                            "decision": "conflicts_with",
                            "code": "owner_confirmation_expired",
                            "conflict_id": str(conflict.id),
                        }
                        await settle_explicit_obligation(
                            session, candidate, now=now, code="owner_confirmation_expired"
                        )
        return len(obligations)

    @staticmethod
    async def _evidence_live(
        session: AsyncSession, candidate: MemoryCandidateRow
    ) -> bool:
        if not candidate.evidence_set_hash:
            return False
        refs = list(
            (
                await session.execute(
                    select(MemoryCandidateEvidenceRow).where(
                        MemoryCandidateEvidenceRow.candidate_id == candidate.id
                    )
                )
            ).scalars()
        )
        if not refs:
            return False
        turns = list(
            (
                await session.execute(
                    select(TurnRow).where(
                        TurnRow.id.in_({ref.turn_id for ref in refs})
                    )
                )
            ).scalars()
        )
        by_id = {turn.id: turn for turn in turns}
        return all(
            ref.turn_id in by_id and _evidence_turn_admissible(candidate, by_id[ref.turn_id])
            for ref in refs
        )

    @staticmethod
    async def _close_stale_attempts(
        session: AsyncSession,
        candidate_id: UUID,
        now: datetime,
        *,
        status: str,
    ) -> None:
        attempts = list(
            (
                await session.execute(
                    select(MemoryReconciliationAttemptRow)
                    .where(
                        MemoryReconciliationAttemptRow.candidate_id == candidate_id,
                        MemoryReconciliationAttemptRow.completed_at.is_(None),
                    )
                    .with_for_update()
                )
            ).scalars()
        )
        for attempt in attempts:
            attempt.status = status
            attempt.completed_at = now
            attempt.result_json = {"result": status}



class DerivedMemoryPublisher:
    """The only V1 path from a candidate to derived durable memory."""

    def __init__(
        self,
        factory: async_sessionmaker,
        *,
        short_term_review_hours: int = 24,
        short_term_expiry_days: int = 14,
    ) -> None:
        self.factory = factory
        self.writer = SharedStateWriter(factory)
        self.short_term_review_hours = max(1, min(int(short_term_review_hours), 168))
        self.short_term_expiry_days = max(1, min(int(short_term_expiry_days), 90))

    async def publish(
        self,
        claim: CandidateLease,
        snapshot: EvaluationSnapshot,
        decision: ReconciliationDecision,
        *,
        operation_id: UUID | None = None,
    ) -> SharedWriteReceipt:
        operation_id = operation_id or uuid4()
        envelope = SharedWriteEnvelope(
            operation_id=operation_id,
            resource_type=_MEMORY_RESOURCE_TYPE,
            resource_id=_MEMORY_RESOURCE_ID,
            operation="reconcile_candidate",
            expected_version=snapshot.memory_revision,
            payload={
                "candidate_id": str(claim.candidate_id),
                "attempt_id": str(claim.attempt_id),
                "lease_token": str(claim.lease_token),
                "semantic_decision": decision.decision,
                "target_memory_id": (
                    str(decision.target_memory_id)
                    if decision.target_memory_id is not None
                    else None
                ),
            },
            actor="memory_reconciler",
            source_transcript_id=snapshot.source_transcript_id,
            source_turn_id=snapshot.source_turn_id,
        )

        async def mutate(
            session: AsyncSession, _current_version: int
        ) -> SharedWriteMutation:
            now = utcnow()
            candidate = (
                await session.execute(
                    select(MemoryCandidateRow)
                    .where(MemoryCandidateRow.id == claim.candidate_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            attempt = (
                await session.execute(
                    select(MemoryReconciliationAttemptRow)
                    .where(MemoryReconciliationAttemptRow.id == claim.attempt_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if attempt is None:
                raise ValueError("reconciliation attempt disappeared")
            if (
                attempt.lease_token != claim.lease_token
                or attempt.completed_at is not None
                or candidate is not None
                and int(candidate.state_version or 0)
                != int(attempt.candidate_state_version or 0)
            ):
                return SharedWriteMutation(
                    mutated=False, result={"result": "lease_conflict"}
                )
            attempt.operation_id = operation_id
            attempt.semantic_decision = decision.decision
            attempt.target_memory_id = decision.target_memory_id

            if candidate is None:
                attempt.status = "lease_conflict"
                attempt.completed_at = now
                attempt.result_json = {"result": "lease_conflict"}
                return SharedWriteMutation(
                    mutated=False, result={"result": "lease_conflict"}
                )

            if (
                candidate.invalidated_at is not None
                or candidate.content is None
                or candidate.fingerprint is None
                or candidate.status == "invalidated"
            ):
                if candidate.lease_token == claim.lease_token:
                    candidate.status = "invalidated"
                    candidate.lease_token = None
                    candidate.leased_until = None
                    candidate.review_after = None
                    candidate.processed_at = now
                attempt.status = "invalidated"
                attempt.completed_at = now
                attempt.result_json = {"result": "invalidated"}
                return SharedWriteMutation(
                    mutated=False, result={"result": "invalidated"}
                )

            if (
                candidate.status != "leased"
                or candidate.lease_token != claim.lease_token
                or candidate.leased_until is None
                or candidate.leased_until <= now
            ):
                attempt.status = "lease_conflict"
                attempt.completed_at = now
                attempt.result_json = {"result": "lease_conflict"}
                return SharedWriteMutation(
                    mutated=False, result={"result": "lease_conflict"}
                )

            evidence_rows = await self._live_evidence_rows(session, candidate)
            if not evidence_rows:
                candidate.status = "invalidated"
                candidate.invalidated_at = now
                candidate.lease_token = None
                candidate.leased_until = None
                candidate.review_after = None
                candidate.processed_at = now
                candidate.decision_json = {
                    "decision": "invalidated",
                    "code": "evidence_unavailable",
                }
                attempt.status = "invalidated"
                attempt.completed_at = now
                attempt.result_json = {
                    "result": "invalidated",
                    "code": "evidence_unavailable",
                }
                return SharedWriteMutation(
                    mutated=False,
                    result={"result": "invalidated", "code": "evidence_unavailable"},
                )
            primary_evidence_turn_id = next(
                (row.turn_id for row in evidence_rows if row.principal == "owner"),
                evidence_rows[0].turn_id,
            )
            _advance_candidate_state(candidate, attempt)

            transcript = (
                await session.execute(
                    select(TranscriptRow)
                    .where(TranscriptRow.id == snapshot.source_transcript_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            source_turn = await session.get(TurnRow, snapshot.source_turn_id)
            if (
                transcript is None
                or source_turn is None
                or int(transcript.content_revision or 0) != snapshot.source_revision
            ):
                candidate.status = "pending"
                candidate.lease_token = None
                candidate.leased_until = None
                attempt.status = "source_conflict"
                attempt.completed_at = now
                attempt.result_json = {"result": "source_conflict"}
                return SharedWriteMutation(
                    mutated=False, result={"result": "source_conflict"}
                )

            if decision.decision == "discard":
                self._complete_candidate(
                    candidate, attempt, now, decision="discard", result="discarded"
                )
                return SharedWriteMutation(
                    mutated=False, result={"result": "discarded"}
                )

            if decision.decision == "retain_short_term":
                candidate.status = "retained_short_term"
                candidate.lease_token = None
                candidate.leased_until = None
                candidate.attempt_count = 0
                candidate.review_after = now + timedelta(
                    hours=self.short_term_review_hours
                )
                if candidate.expires_at is None:
                    candidate.expires_at = now + timedelta(
                        days=self.short_term_expiry_days
                    )
                if (
                    candidate.review_after is not None
                    and candidate.expires_at is not None
                    and candidate.review_after > candidate.expires_at
                ):
                    candidate.review_after = candidate.expires_at
                candidate.processed_at = now
                candidate.decision_json = {"decision": "retain_short_term"}
                attempt.status = "committed"
                attempt.completed_at = now
                attempt.result_json = {"result": "retained_short_term"}
                return SharedWriteMutation(
                    mutated=False, result={"result": "retained_short_term"}
                )

            if not snapshot.publication_allowed:
                return self._block(
                    candidate, attempt, now, "scope_not_publishable"
                )

            if (
                (decision.durability or candidate.durability) == "short_term"
                and decision.decision in {"create", "merge", "supersede"}
            ):
                return self._block(
                    candidate, attempt, now, "short_term_not_durable"
                )

            evaluated_active_ids = {
                UUID(str(item)) for item in snapshot.evidence_refs.get("active_memory_ids", [])
            }
            evaluated_legacy_ids = {
                UUID(str(item))
                for item in snapshot.evidence_refs.get("legacy_unverified_target_ids", [])
            }
            reconciliation_record_raw = snapshot.evidence_refs.get("reconciliation_record_id")
            verification_record_id = (
                UUID(str(reconciliation_record_raw))
                if reconciliation_record_raw is not None
                else None
            )
            owner_assertion_raw = (candidate.decision_json or {}).get(
                "owner_assertion_turn_id"
            )
            owner_assertion_turn_id = (
                UUID(str(owner_assertion_raw)) if owner_assertion_raw else None
            )
            if owner_assertion_turn_id is not None:
                owner_assertion = await session.get(TurnRow, owner_assertion_turn_id)
                if (
                    owner_assertion is None
                    or owner_assertion.deleted_at is not None
                    or owner_assertion.actor != "owner"
                ):
                    return self._block(
                        candidate, attempt, now, "owner_assertion_evidence_invalid"
                    )

            if decision.decision == "await_owner":
                imported_review = candidate.origin not in {
                    "conversation", "owner_statement", "owner_review_confirmation"
                }
                obligation = MemoryObligationRow(
                    kind=("memory_review" if imported_review else "memory_confirmation"),
                    status="pending",
                    subject_type="memory_candidate",
                    subject_id=candidate.id,
                    source_transcript_id=candidate.source_transcript_id,
                    source_turn_id=candidate.source_turn_id,
                    origin=candidate.origin,
                    expires_at=(None if imported_review else now + timedelta(days=7)),
                )
                session.add(obligation)
                await session.flush()
                candidate.status = "awaiting_owner"
                candidate.lease_token = None
                candidate.leased_until = None
                candidate.processed_at = now
                candidate.decision_json = {
                    "decision": "awaiting_owner",
                    "obligation_id": str(obligation.id),
                    "reason_code": decision.reason,
                }
                attempt.status = "awaiting_owner"
                attempt.completed_at = now
                attempt.result_json = {
                    "result": "awaiting_owner",
                    "obligation_id": str(obligation.id),
                    "reason_code": decision.reason,
                }
                return SharedWriteMutation(
                    result={
                        "result": "awaiting_owner",
                        "obligation_id": str(obligation.id),
                    }
                )

            if decision.decision == "conflict":
                if decision.target_memory_id not in evaluated_active_ids:
                    return self._block(
                        candidate, attempt, now, "target_not_in_evaluated_snapshot"
                    )
                target = await session.get(DurableMemoryRow, decision.target_memory_id)
                if target is None or target.status != ACTIVE:
                    return self._block(candidate, attempt, now, "conflict_target_invalid")
                conflict = MemoryConflictRow(
                    candidate_id=candidate.id,
                    reconciliation_id=UUID(
                        str(snapshot.evidence_refs.get("reconciliation_record_id"))
                    ),
                    target_memory_id=target.id,
                    status="open",
                    proposed_content=clean_memory_content(
                        decision.content or candidate.content
                    ),
                    reason_code=decision.reason,
                )
                session.add(conflict)
                await session.flush()
                obligation = MemoryObligationRow(
                    kind="memory_conflict",
                    status="pending",
                    subject_type="memory_conflict",
                    subject_id=conflict.id,
                    source_transcript_id=candidate.source_transcript_id,
                    source_turn_id=candidate.source_turn_id,
                    origin=candidate.origin,
                    resolution_json=(
                        {"trigger_reason": decision.reason} if decision.reason else {}
                    ),
                    expires_at=now + timedelta(days=14),
                )
                session.add(obligation)
                await session.flush()
                candidate.status = "awaiting_owner"
                candidate.lease_token = None
                candidate.leased_until = None
                candidate.processed_at = now
                candidate.decision_json = {
                    "decision": "conflicts_with",
                    "target_memory_id": str(target.id),
                    "conflict_id": str(conflict.id),
                    "obligation_id": str(obligation.id),
                }
                attempt.status = "awaiting_owner"
                attempt.completed_at = now
                attempt.target_memory_id = target.id
                attempt.result_json = {
                    "result": "conflict",
                    "target_memory_id": str(target.id),
                    "conflict_id": str(conflict.id),
                    "obligation_id": str(obligation.id),
                }
                return SharedWriteMutation(
                    result=dict(attempt.result_json)
                )

            if decision.decision == "equivalent":
                if decision.target_memory_id not in evaluated_active_ids:
                    return self._block(
                        candidate, attempt, now, "target_not_in_evaluated_snapshot"
                    )
                target = await session.get(
                    DurableMemoryRow, decision.target_memory_id
                )
                if (
                    target is None
                    or target.status != ACTIVE
                    or not self._target_applicable(
                        target, candidate, decision_scope=decision.scope
                    )
                ):
                    return self._block(
                        candidate, attempt, now, "equivalent_target_invalid"
                    )
                lineage_added = await self._add_candidate_lineage(
                    session, target.id, candidate
                )
                if owner_assertion_turn_id is not None:
                    target.owner_assertion_turn_id = owner_assertion_turn_id
                    session.add(MemoryProvenanceRow(
                        memory_id=target.id,
                        relationship="owner_confirmation",
                        source_turn_id=owner_assertion_turn_id,
                    ))
                    lineage_added = True
                self._complete_candidate(
                    candidate,
                    attempt,
                    now,
                    decision="equivalent",
                    result=(
                        "lineage_added" if lineage_added else "already_represented"
                    ),
                )
                attempt.target_memory_id = target.id
                return SharedWriteMutation(
                    mutated=lineage_added,
                    result={
                        "result": (
                            "lineage_added"
                            if lineage_added
                            else "already_represented"
                        ),
                        "memory_id": str(target.id),
                    },
                )

            proposed_content = clean_memory_content(
                decision.content or candidate.content
            )
            proposed_fingerprint = memory_fingerprint(proposed_content)
            explicit_operation = str(
                snapshot.candidate.get("explicit_operation") or ""
            )
            explicit_change_type = str(
                snapshot.candidate.get("explicit_change_type") or ""
            )
            explicit_target_raw = snapshot.candidate.get("explicit_target_memory_id")
            explicit_target_id = (
                UUID(str(explicit_target_raw)) if explicit_target_raw else None
            )
            restriction = (
                await session.execute(
                    select(DurableMemoryRow)
                    .where(
                        DurableMemoryRow.fingerprint == proposed_fingerprint,
                        DurableMemoryRow.grounding_status == "verified",
                        DurableMemoryRow.suppresses_recall.is_(True),
                        DurableMemoryRow.status.in_([RETIRED, SUPERSEDED]),
                    )
                    .order_by(DurableMemoryRow.updated_at.desc(), DurableMemoryRow.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if restriction is not None and (
                restriction.status == RETIRED
                or decision.decision != "historical_predecessor"
            ):
                return self._block(
                    candidate, attempt, now, "owner_lifecycle_restriction"
                )

            repository = DurableMemoryRepository(session)
            effective_scope = decision.scope or candidate.scope
            effective_scope_key = (
                "owner"
                if effective_scope == "cross_chat"
                else (
                    f"chat:{candidate.source_transcript_id}"
                    if effective_scope == "chat"
                    else f"project-unresolved:{candidate.source_transcript_id}"
                )
            )
            classification = {
                "record_kind": "derived",
                "memory_kind": decision.memory_kind or candidate.kind,
                "scope": effective_scope,
                "scope_key": effective_scope_key,
                "durability": decision.durability or candidate.durability,
                "subject": candidate.subject,
                "namespace": candidate.namespace,
            }

            if decision.decision == "ground_legacy":
                if decision.target_memory_id not in evaluated_legacy_ids:
                    return self._block(
                        candidate, attempt, now, "legacy_target_not_in_evaluated_snapshot"
                    )
                target = await session.get(DurableMemoryRow, decision.target_memory_id)
                if (
                    target is None
                    or target.status != ACTIVE
                    or target.grounding_status != "legacy_unverified"
                    or _owner_directed(target)
                    or not target.record_kind.startswith("derived")
                    or not self._same_scope(
                        target, candidate, decision_scope=decision.scope
                    )
                ):
                    return self._block(
                        candidate, attempt, now, "legacy_grounding_target_invalid"
                    )
                existing = await repository.active_by_fingerprint(proposed_fingerprint)
                if existing is not None and existing.id != target.id:
                    return self._block(
                        candidate, attempt, now, "legacy_grounding_duplicate_exists"
                    )
                target.content = proposed_content
                target.fingerprint = proposed_fingerprint
                target.grounding_status = "verified"
                target.verification_record_id = verification_record_id
                target.owner_assertion_turn_id = owner_assertion_turn_id
                target.memory_kind = classification["memory_kind"]
                target.scope = classification["scope"]
                target.scope_key = classification["scope_key"]
                target.durability = classification["durability"]
                target.subject = candidate.subject
                target.namespace = candidate.namespace
                target.source_transcript_id = candidate.source_transcript_id
                target.source_turn_id = primary_evidence_turn_id
                target.valid_from = decision.event_valid_from
                target.valid_to = decision.event_valid_to
                target.updated_at = now
                _clear_embedding(target)
                await self._add_candidate_lineage(session, target.id, candidate)
                review = (
                    await session.execute(
                        select(MemoryObligationRow).where(
                            MemoryObligationRow.kind == "memory_review",
                            MemoryObligationRow.status == "pending",
                            MemoryObligationRow.subject_type == "durable_memory",
                            MemoryObligationRow.subject_id == target.id,
                        )
                    )
                ).scalar_one_or_none()
                if review is not None:
                    review.status = "resolved"
                    review.resolution_code = "grounded_by_evidence"
                    review.resolution_json = {"candidate_id": str(candidate.id)}
                    review.resolved_at = now
                self._complete_candidate(
                    candidate, attempt, now,
                    decision="ground_legacy", result="legacy_grounded"
                )
                attempt.target_memory_id = target.id
                return SharedWriteMutation(
                    result={
                        "result": "legacy_grounded",
                        "memory_id": str(target.id),
                    }
                )

            if decision.decision == "historical_predecessor":
                if decision.target_memory_id not in evaluated_active_ids:
                    return self._block(
                        candidate, attempt, now, "target_not_in_evaluated_snapshot"
                    )
                target = await session.get(DurableMemoryRow, decision.target_memory_id)
                if (
                    target is None
                    or target.status != ACTIVE
                    or not self._target_applicable(
                        target, candidate, decision_scope=decision.scope
                    )
                ):
                    return self._block(
                        candidate, attempt, now, "historical_target_invalid"
                    )
                if restriction is not None:
                    linked_to_target = (
                        restriction.superseded_by_id == target.id
                        or target.supersedes_id == restriction.id
                    )
                    if (
                        restriction.status != SUPERSEDED
                        or _owner_directed(restriction)
                        or not restriction.record_kind.startswith("derived")
                        or not self._target_applicable(
                            restriction, candidate, decision_scope=decision.scope
                        )
                        or not linked_to_target
                    ):
                        return self._block(
                            candidate, attempt, now,
                            "historical_lifecycle_restriction",
                        )
                    lineage_added = await self._add_candidate_lineage(
                        session, restriction.id, candidate
                    )
                    self._complete_candidate(
                        candidate,
                        attempt,
                        now,
                        decision="historical_predecessor",
                        result=(
                            "historical_lineage_added"
                            if lineage_added
                            else "historical_already_represented"
                        ),
                    )
                    attempt.target_memory_id = target.id
                    return SharedWriteMutation(
                        mutated=lineage_added,
                        result={
                            "result": (
                                "historical_lineage_added"
                                if lineage_added
                                else "historical_already_represented"
                            ),
                            "memory_id": str(restriction.id),
                            "target_memory_id": str(target.id),
                        },
                    )
                historical = DurableMemoryRow(
                    status=SUPERSEDED,
                    record_kind="derived",
                    grounding_status="verified",
                    verification_record_id=verification_record_id,
                    owner_assertion_turn_id=owner_assertion_turn_id,
                    originating_candidate_id=candidate.id,
                    origin="conversation_verification",
                    memory_kind=classification["memory_kind"],
                    scope=classification["scope"],
                    scope_key=classification["scope_key"],
                    durability=classification["durability"],
                    subject=candidate.subject,
                    namespace=candidate.namespace,
                    content=proposed_content,
                    fingerprint=proposed_fingerprint,
                    suppresses_recall=True,
                    source_transcript_id=candidate.source_transcript_id,
                    source_turn_id=primary_evidence_turn_id,
                    superseded_by_id=target.id,
                    valid_from=decision.event_valid_from,
                    valid_to=target.valid_from,
                )
                session.add(historical)
                await session.flush()
                await self._add_candidate_lineage(session, historical.id, candidate)
                session.add(
                    MemoryProvenanceRow(
                        memory_id=historical.id,
                        relationship="historical_predecessor",
                        source_memory_id=target.id,
                    )
                )
                self._complete_candidate(
                    candidate,
                    attempt,
                    now,
                    decision="historical_predecessor",
                    result="historical_predecessor",
                )
                attempt.target_memory_id = target.id
                return SharedWriteMutation(
                    result={
                        "result": "historical_predecessor",
                        "memory_id": str(historical.id),
                        "target_memory_id": str(target.id),
                    }
                )

            if decision.decision == "create":
                existing = await repository.active_by_fingerprint(
                    proposed_fingerprint
                )
                if existing is not None:
                    return self._block(
                        candidate, attempt, now, "create_target_already_exists"
                    )
                correction_guard: DurableMemoryRow | None = None
                if explicit_operation == "correct" and explicit_target_id is not None:
                    explicit_target = await session.get(
                        DurableMemoryRow, explicit_target_id
                    )
                    if (
                        explicit_target is not None
                        and explicit_target.status in {RETIRED, SUPERSEDED}
                        and explicit_target.record_kind == "owner_correction_guard"
                    ):
                        correction_guard = explicit_target
                    elif explicit_target is not None and explicit_target.status == ACTIVE:
                        return self._block(
                            candidate, attempt, now,
                            "explicit_correction_target_not_reconciled",
                        )
                memory, created = await repository.create_active(
                    proposed_content,
                    source_transcript_id=candidate.source_transcript_id,
                    source_turn_id=primary_evidence_turn_id,
                    supersedes_id=(correction_guard.id if correction_guard else None),
                    valid_from=(
                        None
                        if explicit_operation == "correct"
                        and explicit_change_type == "correction"
                        else decision.event_valid_from
                    ),
                    valid_to=decision.event_valid_to,
                    origin="conversation_verification",
                    grounding_status="verified",
                    verification_record_id=verification_record_id,
                    owner_assertion_turn_id=owner_assertion_turn_id,
                    originating_candidate_id=candidate.id,
                    **classification,
                )
                if not created or memory.record_kind != "derived":
                    return self._block(
                        candidate, attempt, now, "derived_create_refused"
                    )
                await self._add_candidate_lineage(session, memory.id, candidate)
                if correction_guard is not None:
                    correction_guard.superseded_by_id = memory.id
                    session.add(MemoryProvenanceRow(
                        memory_id=memory.id,
                        relationship=(
                            "corrects"
                            if explicit_change_type == "correction"
                            else "supersedes"
                        ),
                        source_memory_id=correction_guard.id,
                    ))
                self._complete_candidate(
                    candidate, attempt, now, decision="create", result="created"
                )
                attempt.target_memory_id = memory.id
                return SharedWriteMutation(
                    result={"result": "created", "memory_id": str(memory.id)}
                )

            if decision.decision not in {"merge", "supersede"}:
                return self._block(
                    candidate, attempt, now, "unsupported_semantic_decision"
                )

            if decision.target_memory_id not in evaluated_active_ids:
                return self._block(
                    candidate, attempt, now, "target_not_in_evaluated_snapshot"
                )
            target = await session.get(DurableMemoryRow, decision.target_memory_id)
            if (
                explicit_operation == "correct"
                and explicit_target_id is not None
                and decision.target_memory_id != explicit_target_id
            ):
                return self._block(
                    candidate, attempt, now, "explicit_correction_target_mismatch"
                )
            explicit_correction_of_target = (
                explicit_operation == "correct"
                and explicit_target_id is not None
                and decision.target_memory_id == explicit_target_id
            )
            if (
                target is None
                or target.status != ACTIVE
                or (
                    not explicit_correction_of_target
                    and (_owner_directed(target) or not target.record_kind.startswith("derived"))
                )
                or not self._same_scope(
                    target, candidate, decision_scope=decision.scope
                )
                or (
                    target.memory_kind is not None
                    and target.memory_kind != (decision.memory_kind or candidate.kind)
                )
            ):
                return self._block(
                    candidate, attempt, now, "derived_target_invalid"
                )

            existing = await repository.active_by_fingerprint(proposed_fingerprint)
            if existing is not None and existing.id != target.id:
                return self._block(
                    candidate, attempt, now, "replacement_already_exists"
                )
            if existing is not None and existing.id == target.id:
                return self._block(
                    candidate, attempt, now, "replacement_unchanged"
                )

            replacement_valid_from = decision.event_valid_from
            if (
                explicit_operation == "correct"
                and explicit_change_type == "correction"
            ):
                replacement_valid_from = None
            replacement, created = await repository.create_active(
                proposed_content,
                source_transcript_id=candidate.source_transcript_id,
                source_turn_id=primary_evidence_turn_id,
                supersedes_id=target.id,
                valid_from=replacement_valid_from,
                valid_to=decision.event_valid_to,
                origin="conversation_verification",
                grounding_status="verified",
                verification_record_id=verification_record_id,
                owner_assertion_turn_id=owner_assertion_turn_id,
                originating_candidate_id=candidate.id,
                **classification,
            )
            if not created or replacement.id == target.id:
                return self._block(
                    candidate, attempt, now, "derived_replacement_refused"
                )
            target.status = SUPERSEDED
            target.suppresses_recall = True
            target.superseded_by_id = replacement.id
            if not (
                explicit_operation == "correct"
                and explicit_change_type == "correction"
            ):
                target.valid_to = decision.event_valid_from or now
            target.updated_at = now
            _clear_embedding(target)
            await self._add_candidate_lineage(session, replacement.id, candidate)
            session.add(
                MemoryProvenanceRow(
                    memory_id=replacement.id,
                    relationship=(
                        "corrects"
                        if explicit_operation == "correct"
                        and explicit_change_type == "correction"
                        else (
                            "merged_from"
                            if decision.decision == "merge"
                            else "supersedes"
                        )
                    ),
                    source_memory_id=target.id,
                )
            )
            self._complete_candidate(
                candidate,
                attempt,
                now,
                decision=decision.decision,
                result="created",
            )
            attempt.target_memory_id = replacement.id
            return SharedWriteMutation(
                result={
                    "result": decision.decision,
                    "memory_id": str(replacement.id),
                    "target_memory_id": str(target.id),
                }
            )

        async def mutate_and_settle(
            session: AsyncSession, current_version: int
        ) -> SharedWriteMutation:
            outcome = await mutate(session, current_version)
            result = dict(outcome.result or {})
            try:
                terminality = PublicationOutcome(str(result.get("result"))).terminality
            except ValueError:
                return outcome
            if terminality not in {
                PublicationTerminality.TERMINAL_SUCCESS,
                PublicationTerminality.TERMINAL_FAILURE,
            }:
                return outcome
            candidate = await session.get(MemoryCandidateRow, claim.candidate_id)
            if candidate is None:
                return outcome
            memory_raw = result.get("memory_id") or result.get("replacement_memory_id")
            await settle_explicit_obligation(
                session,
                candidate,
                now=utcnow(),
                memory_id=UUID(str(memory_raw)) if memory_raw else None,
                code=(str(result.get("code")) if result.get("code") else None),
            )
            return outcome

        return await self.writer.execute(envelope, mutate_and_settle)

    @staticmethod
    async def _live_evidence_rows(
        session: AsyncSession, candidate: MemoryCandidateRow
    ) -> list[MemoryCandidateEvidenceRow]:
        if not candidate.evidence_set_hash:
            return []
        refs = list(
            (
                await session.execute(
                    select(MemoryCandidateEvidenceRow)
                    .where(MemoryCandidateEvidenceRow.candidate_id == candidate.id)
                    .order_by(MemoryCandidateEvidenceRow.ordinal.asc())
                )
            ).scalars()
        )
        if not refs:
            return []
        turns = list(
            (
                await session.execute(
                    select(TurnRow).where(TurnRow.id.in_({ref.turn_id for ref in refs}))
                )
            ).scalars()
        )
        by_id = {turn.id: turn for turn in turns}
        if not all(
            ref.turn_id in by_id and _evidence_turn_admissible(candidate, by_id[ref.turn_id])
            for ref in refs
        ):
            return []
        return refs

    @staticmethod
    async def _add_candidate_lineage(
        session: AsyncSession,
        memory_id: UUID,
        candidate: MemoryCandidateRow,
    ) -> bool:
        refs = list(
            (
                await session.execute(
                    select(MemoryCandidateEvidenceRow)
                    .where(MemoryCandidateEvidenceRow.candidate_id == candidate.id)
                    .order_by(MemoryCandidateEvidenceRow.ordinal.asc())
                )
            ).scalars()
        )
        evidence_ids = {ref.turn_id for ref in refs}
        existing_evidence_ids = (
            set(
                (
                    await session.execute(
                        select(MemoryProvenanceRow.source_turn_id).where(
                            MemoryProvenanceRow.memory_id == memory_id,
                            MemoryProvenanceRow.relationship == "evidence_source",
                            MemoryProvenanceRow.source_turn_id.in_(evidence_ids),
                        )
                    )
                ).scalars()
            )
            if evidence_ids
            else set()
        )
        missing_refs = [ref for ref in refs if ref.turn_id not in existing_evidence_ids]
        if not missing_refs:
            return False
        session.add(
            MemoryProvenanceRow(
                memory_id=memory_id,
                relationship="candidate_source",
                source_candidate_id=candidate.id,
            )
        )
        for ref in missing_refs:
            session.add(
                MemoryProvenanceRow(
                    memory_id=memory_id,
                    relationship="evidence_source",
                    source_candidate_id=candidate.id,
                    source_turn_id=ref.turn_id,
                )
            )
        return True

    @staticmethod
    def _complete_candidate(
        candidate: MemoryCandidateRow,
        attempt: MemoryReconciliationAttemptRow,
        now: datetime,
        *,
        decision: str,
        result: str,
    ) -> None:
        candidate.status = "reconciled" if decision != "discard" else "discarded"
        candidate.lease_token = None
        candidate.leased_until = None
        candidate.processed_at = now
        candidate.decision_json = {"decision": decision, "result": result}
        attempt.status = "committed"
        attempt.completed_at = now
        attempt.result_json = {"result": result}

    @staticmethod
    def _block(
        candidate: MemoryCandidateRow,
        attempt: MemoryReconciliationAttemptRow,
        now: datetime,
        code: str,
    ) -> SharedWriteMutation:
        candidate.status = "blocked"
        candidate.lease_token = None
        candidate.leased_until = None
        candidate.processed_at = now
        candidate.decision_json = {"decision": "blocked", "code": code}
        attempt.status = "committed"
        attempt.completed_at = now
        attempt.result_json = {"result": "blocked", "code": code}
        return SharedWriteMutation(
            mutated=False, result={"result": "blocked", "code": code}
        )

    @staticmethod
    def _target_applicable(
        target: DurableMemoryRow,
        candidate: MemoryCandidateRow,
        *,
        decision_scope: str | None = None,
    ) -> bool:
        scope = decision_scope or candidate.scope
        if scope == "cross_chat":
            return target.scope == "cross_chat"
        if scope == "chat":
            scope_key = f"chat:{candidate.source_transcript_id}"
            return target.scope == "cross_chat" or (
                target.scope == "chat" and target.scope_key == scope_key
            )
        return False

    @staticmethod
    def _same_scope(
        target: DurableMemoryRow,
        candidate: MemoryCandidateRow,
        *,
        decision_scope: str | None = None,
    ) -> bool:
        scope = decision_scope or candidate.scope
        scope_key = (
            "owner"
            if scope == "cross_chat"
            else (
                f"chat:{candidate.source_transcript_id}"
                if scope == "chat"
                else f"project-unresolved:{candidate.source_transcript_id}"
            )
        )
        return target.scope == scope and target.scope_key == scope_key


class MemoryReconciliationService:
    """Evidence-first memory verification with explicit persisted semantic stages."""

    _SENSITIVE_CATEGORIES: ClassVar[set[str]] = {
        "health", "credential", "credentials", "financial", "legal"
    }
    _SUPPORTED_KINDS: ClassVar[set[str]] = {
        "identity", "preference", "fact", "decision", "relationship",
        "procedure", "project_state", "intent",
    }

    def __init__(
        self,
        factory: async_sessionmaker,
        model: ReconciliationModel,
        *,
        lease_seconds: int = 180,
        max_attempts: int = 3,
        batch_size: int = 8,
        short_term_review_hours: int = 24,
        short_term_expiry_days: int = 14,
    ) -> None:
        self.factory = factory
        self.model = model
        self.batch_size = max(1, min(int(batch_size), 32))
        self.max_attempts = max(1, min(int(max_attempts), 10))
        self.leases = MemoryCandidateLeaseRepository(
            factory,
            lease_seconds=lease_seconds,
            max_attempts=max_attempts,
        )
        self.publisher = DerivedMemoryPublisher(
            factory,
            short_term_review_hours=short_term_review_hours,
            short_term_expiry_days=short_term_expiry_days,
        )

    async def run_once(self) -> ReconciliationRunResult:
        claims, expired = await self.leases.claim_batch(limit=self.batch_size)
        stats = {
            "claimed": len(claims),
            "reconciled": 0,
            "retained_short_term": 0,
            "discarded": 0,
            "blocked": 0,
            "version_conflicts": 0,
            "source_conflicts": 0,
            "lease_conflicts": 0,
            "failures": 0,
            "expired": expired,
        }
        self._contract_notes = {}
        for claim in claims:
            try:
                snapshot = await self._load_snapshot(claim)
                reusable = await self._reusable_verification(snapshot)
                if reusable is None:
                    reading = await self._independent_read(snapshot)
                    reading_row = await self._persist_reading(
                        claim, snapshot, reading
                    )
                    comparison = await self._compare(
                        snapshot, reading_row, reading
                    )
                    comparison_row = await self._persist_comparison(
                        claim, reading_row, comparison
                    )
                else:
                    reading_row, reading, comparison_row, comparison = reusable

                if (
                    not reading.extracted_claims
                    or comparison.verdict in {"contradicted", "insufficient_evidence"}
                ):
                    await self._finish_epistemic_rejection(
                        claim,
                        code=(
                            "no_supported_claim"
                            if not reading.extracted_claims
                            else comparison.verdict
                        ),
                    )
                    stats["discarded"] += 1
                    continue

                result, conflicts = await self._reconcile_policy_publish(
                    claim,
                    snapshot,
                    reading_row,
                    reading,
                    comparison_row,
                    comparison,
                )
                stats["version_conflicts"] += conflicts
                try:
                    outcome = PublicationOutcome(result)
                except ValueError:
                    stats["failures"] += 1
                    await self._release_failed(
                        claim, code="unknown_publish_result"
                    )
                    continue
                if outcome.bucket == PublicationBucket.RECONCILED:
                    stats["reconciled"] += 1
                elif outcome.bucket == PublicationBucket.RETAINED_SHORT_TERM:
                    stats["retained_short_term"] += 1
                elif outcome.bucket == PublicationBucket.DISCARDED:
                    stats["discarded"] += 1
                elif outcome.bucket == PublicationBucket.BLOCKED:
                    stats["blocked"] += 1
                elif outcome.bucket == PublicationBucket.SOURCE_CONFLICT:
                    stats["source_conflicts"] += 1
                elif outcome.bucket == PublicationBucket.LEASE_CONFLICT:
                    stats["lease_conflicts"] += 1
                elif outcome.bucket == PublicationBucket.VERSION_CONFLICT:
                    # Bounded CAS retries were exhausted; the candidate was requeued.
                    pass
            except CandidateSnapshotError:
                stats["failures"] += 1
                await self._release_failed(claim, code="snapshot_stale")
            except Exception:
                logger.exception(
                    "memory reconciliation failed for candidate %s", claim.candidate_id
                )
                stats["failures"] += 1
                await self._release_failed(
                    claim, code="model_or_publish_error"
                )
        return ReconciliationRunResult(
            **stats, contract_notes=dict(sorted(self._contract_notes.items()))
        )

    def _note_contract(self, stage: str, notes: dict[str, int]) -> None:
        if not notes:
            return
        store = getattr(self, "_contract_notes", None)
        if store is None:
            store = self._contract_notes = {}
        for key, count in notes.items():
            store[f"{stage}.{key}"] = store.get(f"{stage}.{key}", 0) + int(count)
        logger.warning("memory verifier %s output folded: %s", stage, notes)

    @staticmethod
    async def _explicit_command_context(
        session: AsyncSession, candidate_id: UUID
    ) -> dict[str, object]:
        row = (
            await session.execute(
                select(MemoryCommandRow)
                .join(
                    MemoryObligationRow,
                    MemoryObligationRow.command_id == MemoryCommandRow.id,
                )
                .where(
                    MemoryObligationRow.subject_type == "memory_candidate",
                    MemoryObligationRow.subject_id == candidate_id,
                    MemoryObligationRow.kind.in_(EXPLICIT_OBLIGATION_KINDS),
                )
                .order_by(MemoryObligationRow.created_at.asc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is None:
            return {}
        arguments = dict(row.arguments_json or {})
        return {
            "explicit_operation": row.operation,
            "explicit_target_memory_id": (
                str(row.target_memory_id) if row.target_memory_id is not None else None
            ),
            "explicit_change_type": arguments.get("change_type"),
        }

    async def _load_snapshot(self, claim: CandidateLease) -> EvaluationSnapshot:
        async with self.factory() as session, session.begin():
            await session.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            )
            now = datetime.now(UTC)
            candidate = (
                await session.execute(
                    select(MemoryCandidateRow)
                    .where(MemoryCandidateRow.id == claim.candidate_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            attempt = (
                await session.execute(
                    select(MemoryReconciliationAttemptRow)
                    .where(MemoryReconciliationAttemptRow.id == claim.attempt_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if (
                candidate is None
                or attempt is None
                or candidate.status != "leased"
                or candidate.lease_token != claim.lease_token
                or attempt.lease_token != claim.lease_token
                or candidate.leased_until is None
                or candidate.leased_until <= now
                or candidate.invalidated_at is not None
                or candidate.content is None
                or candidate.fingerprint is None
                or not candidate.evidence_set_hash
            ):
                raise CandidateSnapshotError("candidate lease is not current")

            transcript = await session.get(
                TranscriptRow, candidate.source_transcript_id
            )
            trigger_turn = await session.get(TurnRow, candidate.source_turn_id)
            if transcript is None or trigger_turn is None:
                raise CandidateSnapshotError("candidate trigger is unavailable")

            evidence_rows = list(
                (
                    await session.execute(
                        select(MemoryCandidateEvidenceRow)
                        .where(
                            MemoryCandidateEvidenceRow.candidate_id == candidate.id
                        )
                        .order_by(MemoryCandidateEvidenceRow.ordinal.asc())
                    )
                ).scalars()
            )
            if not evidence_rows:
                raise CandidateSnapshotError("candidate has no canonical evidence")
            evidence_turns = list(
                (
                    await session.execute(
                        select(TurnRow).where(
                            TurnRow.id.in_({row.turn_id for row in evidence_rows})
                        )
                    )
                ).scalars()
            )
            by_id = {row.id: row for row in evidence_turns}
            if not all(
                ref.turn_id in by_id
                and _evidence_turn_admissible(candidate, by_id[ref.turn_id])
                for ref in evidence_rows
            ):
                raise CandidateSnapshotError("candidate evidence is unavailable")

            evidence_items = [
                self._evidence_projection(ref, by_id[ref.turn_id])
                for ref in evidence_rows
            ]
            source_revision = int(transcript.content_revision or 0)
            memory_revision = await _memory_revision(session)
            explicit_context = await self._explicit_command_context(session, candidate.id)
            explicit_target_raw = explicit_context.get("explicit_target_memory_id")
            explicit_target_id = (
                UUID(str(explicit_target_raw)) if explicit_target_raw else None
            )
            active, restricted, legacy = await self._related_memories(
                session, candidate, explicit_target_memory_id=explicit_target_id
            )
            evidence_refs = {
                "trigger_turn_id": str(candidate.source_turn_id),
                "evidence_set_hash": candidate.evidence_set_hash,
                "evidence_turn_ids": [str(row.turn_id) for row in evidence_rows],
                "source_provider_evidence_id": (
                    str(candidate.source_provider_evidence_id)
                    if candidate.source_provider_evidence_id is not None
                    else None
                ),
                "active_memory_ids": [str(row.id) for row in active],
                "restricted_memory_ids": [str(row.id) for row in restricted],
                "legacy_unverified_target_ids": [str(row.id) for row in legacy],
            }
            candidate_payload = {
                "kind": candidate.kind,
                "content": candidate.content,
                "scope": candidate.scope,
                "scope_key": candidate.scope_key,
                "confidence": float(candidate.confidence),
                "durability": candidate.durability,
                "subject": candidate.subject,
                "namespace": candidate.namespace,
                "intake_path": candidate.intake_path,
                "origin": candidate.origin,
                "temporal_horizon_at": candidate.temporal_horizon_at,
                "owner_confirmation_granted": bool(
                    (candidate.decision_json or {}).get("owner_confirmation_granted")
                ),
                **explicit_context,
            }
            publication_allowed = (
                candidate.scope in {"chat", "cross_chat"}
                and not str(candidate.scope_key or "").startswith(
                    "project-unresolved:"
                )
            )
            return EvaluationSnapshot(
                candidate_id=candidate.id,
                source_transcript_id=candidate.source_transcript_id,
                source_turn_id=candidate.source_turn_id,
                source_provider_evidence_id=candidate.source_provider_evidence_id,
                source_revision=source_revision,
                memory_revision=memory_revision,
                candidate=candidate_payload,
                source_text=_source_text(trigger_turn),
                evidence_items=evidence_items,
                active_memories=[_project_for_model(row) for row in active],
                restricted_memories=[
                    _project_for_model(row) for row in restricted
                ],
                legacy_unverified_targets=[
                    _project_for_model(row) for row in legacy
                ],
                evidence_refs=evidence_refs,
                publication_allowed=publication_allowed,
            )

    @staticmethod
    def _evidence_projection(
        ref: MemoryCandidateEvidenceRow, turn: TurnRow
    ) -> dict[str, object]:
        text_value = _source_text(turn)
        if ref.span_ref:
            try:
                kind, raw_index = ref.span_ref.split(":", 1)
                index = int(raw_index)
            except (TypeError, ValueError):
                raise CandidateSnapshotError("evidence span is malformed") from None
            if kind != "text" or index < 0 or index >= len(turn.blocks or []):
                raise CandidateSnapshotError("evidence span is unavailable")
            block = (turn.blocks or [])[index]
            if not isinstance(block, dict) or block.get("type") != "text":
                raise CandidateSnapshotError("evidence span is not canonical text")
            text_value = _clip(block.get("text"), 4_000)
        return {
            "turn_id": str(turn.id),
            "span_ref": ref.span_ref,
            "principal": ref.principal,
            "actor": turn.actor,
            "sequence": int(turn.sequence),
            "recorded_at": turn.created_at,
            "text": text_value,
        }

    async def _related_memories(
        self,
        session: AsyncSession,
        candidate: MemoryCandidateRow,
        *,
        explicit_target_memory_id: UUID | None = None,
    ) -> tuple[list[DurableMemoryRow], list[DurableMemoryRow], list[DurableMemoryRow]]:
        return await self._related_memories_for(
            session,
            candidate,
            kind=candidate.kind,
            scope=candidate.scope,
            scope_key=candidate.scope_key,
            explicit_target_memory_id=explicit_target_memory_id,
        )

    async def _related_memories_for(
        self,
        session: AsyncSession,
        candidate: MemoryCandidateRow,
        *,
        kind: str,
        scope: str,
        scope_key: str | None,
        explicit_target_memory_id: UUID | None = None,
    ) -> tuple[list[DurableMemoryRow], list[DurableMemoryRow], list[DurableMemoryRow]]:
        if scope == "cross_chat":
            scope_condition = DurableMemoryRow.scope == "cross_chat"
        elif scope == "chat":
            effective_key = scope_key or f"chat:{candidate.source_transcript_id}"
            scope_condition = or_(
                DurableMemoryRow.scope == "cross_chat",
                and_(
                    DurableMemoryRow.scope == "chat",
                    DurableMemoryRow.scope_key == effective_key,
                ),
            )
        else:
            effective_key = scope_key or f"project-unresolved:{candidate.source_transcript_id}"
            scope_condition = and_(
                DurableMemoryRow.scope == "project",
                DurableMemoryRow.scope_key == effective_key,
            )

        durable_fingerprint = memory_fingerprint(candidate.content or "")
        exact = (
            await session.execute(
                select(DurableMemoryRow).where(
                    DurableMemoryRow.status == ACTIVE,
                    DurableMemoryRow.grounding_status == "verified",
                    DurableMemoryRow.fingerprint == durable_fingerprint,
                    scope_condition,
                )
            )
        ).scalars().all()
        related_conditions = [DurableMemoryRow.memory_kind == kind]
        if candidate.subject:
            related_conditions.append(DurableMemoryRow.subject == candidate.subject)
        if candidate.namespace:
            related_conditions.append(
                DurableMemoryRow.namespace == candidate.namespace
            )
        related = (
            await session.execute(
                select(DurableMemoryRow)
                .where(
                    DurableMemoryRow.status == ACTIVE,
                    DurableMemoryRow.grounding_status == "verified",
                    scope_condition,
                    or_(*related_conditions),
                )
                .order_by(
                    DurableMemoryRow.updated_at.desc(),
                    DurableMemoryRow.id.desc(),
                )
                .limit(16)
            )
        ).scalars().all()
        active_by_id = {row.id: row for row in [*exact, *related]}

        restricted_conditions = [
            DurableMemoryRow.fingerprint == durable_fingerprint,
            DurableMemoryRow.memory_kind == kind,
        ]
        if candidate.subject:
            restricted_conditions.append(DurableMemoryRow.subject == candidate.subject)
        if candidate.namespace:
            restricted_conditions.append(
                DurableMemoryRow.namespace == candidate.namespace
            )
        restricted = (
            await session.execute(
                select(DurableMemoryRow)
                .where(
                    DurableMemoryRow.status.in_([RETIRED, SUPERSEDED]),
                    DurableMemoryRow.grounding_status == "verified",
                    DurableMemoryRow.suppresses_recall.is_(True),
                    DurableMemoryRow.content.is_not(None),
                    scope_condition,
                    or_(*restricted_conditions),
                )
                .order_by(
                    DurableMemoryRow.updated_at.desc(),
                    DurableMemoryRow.id.desc(),
                )
                .limit(16)
            )
        ).scalars().all()
        legacy = list((
            await session.execute(
                select(DurableMemoryRow)
                .where(
                    DurableMemoryRow.status == ACTIVE,
                    DurableMemoryRow.grounding_status == "legacy_unverified",
                    DurableMemoryRow.record_kind.like("derived%"),
                    DurableMemoryRow.content.is_not(None),
                    scope_condition,
                    or_(*related_conditions),
                )
                .order_by(
                    DurableMemoryRow.updated_at.desc(),
                    DurableMemoryRow.id.desc(),
                )
                .limit(16)
            )
        ).scalars().all())
        restricted = list(restricted)
        if explicit_target_memory_id is not None:
            explicit_target = await session.get(DurableMemoryRow, explicit_target_memory_id)
            if explicit_target is not None and explicit_target.content is not None:
                if (
                    explicit_target.status == ACTIVE
                    and explicit_target.grounding_status == "verified"
                ):
                    active_by_id[explicit_target.id] = explicit_target
                elif (
                    explicit_target.status in {RETIRED, SUPERSEDED}
                    and explicit_target.grounding_status == "verified"
                    and explicit_target.suppresses_recall
                    and all(row.id != explicit_target.id for row in restricted)
                ):
                    restricted.append(explicit_target)
                elif (
                    explicit_target.status == ACTIVE
                    and explicit_target.grounding_status == "legacy_unverified"
                    and all(row.id != explicit_target.id for row in legacy)
                ):
                    legacy.append(explicit_target)
        return list(active_by_id.values()), restricted, legacy

    async def _independent_read(
        self, snapshot: EvaluationSnapshot
    ) -> IndependentReading:
        instructions = (
            "You are the blind evidence-reading stage of Atlas memory verification. "
            "You have not been shown any memory proposal and must not infer one. Read only "
            "the canonical evidence supplied below. Each item has a deterministic principal: "
            "owner, assistant, tool, or other. A tool/document statement is not an owner fact. "
            "Extract only claims actually supported by the evidence. If the evidence supports "
            "no memory-worthy claim, return an empty extracted_claims list and null category, "
            "scope, durability, and event times. event_valid_from/event_valid_to are semantic "
            "event times, not database write times; use recorded_at only when the statement "
            "itself establishes a current state at that observation. For the supported claim set, "
            "also classify claim_principal as owner when the claim "
            "is directly asserted by owner-principal evidence, external when its authority comes "
            "from a tool/document/third-party source even if the owner asks Atlas to remember it, "
            "mixed when both are materially required, or unknown when authority cannot be resolved. "
            "Do not treat an owner's storage request as evidence that an external claim is an owner fact. "
            "Return exactly one JSON object with keys extracted_claims, category, scope, durability, "
            "claim_principal, event_valid_from, event_valid_to. Use only these exact values: "
            "extracted_claims is a list of at most eight strings (if more claims are supported, return "
            "the eight most memory-worthy); scope is chat, project, cross_chat, or null (never owner, "
            "personal, or global); durability is short_term, long_term, or null (never episodic or "
            "permanent); claim_principal is owner, external, mixed, unknown, or null; category is one "
            "short lowercase word such as preference, fact, identity, decision, relationship, "
            "procedure, project_state, or intent; event times are ISO-8601 or null. Any other value "
            "is discarded. No proposal comparison, policy judgment, or markdown."
        )
        raw = await self.model.complete_text(
            instructions=instructions,
            messages=[{
                "role": "user",
                "content": json.dumps(
                    {"canonical_evidence": snapshot.evidence_items},
                    ensure_ascii=False,
                    separators=(",", ":"),
                    default=str,
                ),
            }],
        )
        payload, notes = normalize_reading_payload(self._json_object(raw, "blind reading"))
        self._note_contract("blind_reading", notes)
        return IndependentReading.model_validate(payload)

    async def _persist_reading(
        self,
        claim: CandidateLease,
        snapshot: EvaluationSnapshot,
        reading: IndependentReading,
    ) -> MemoryIndependentReadingRow:
        async with self.factory() as session, session.begin():
            candidate, attempt = await self._locked_live_claim(session, claim)
            existing = (
                await session.execute(
                    select(MemoryIndependentReadingRow).where(
                        MemoryIndependentReadingRow.attempt_id == claim.attempt_id
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                return existing
            _advance_candidate_state(candidate, attempt)
            row = MemoryIndependentReadingRow(
                candidate_id=candidate.id,
                attempt_id=attempt.id,
                evidence_set_hash=str(candidate.evidence_set_hash),
                source_revision=snapshot.source_revision,
                extracted_claims_json=list(reading.extracted_claims),
                category=reading.category,
                scope=reading.scope,
                durability=reading.durability,
                claim_principal=reading.claim_principal,
                event_valid_from=reading.event_valid_from,
                event_valid_to=reading.event_valid_to,
                verifier_model=str(getattr(self.model, "model", "") or "") or None,
            )
            session.add(row)
            attempt.status = "blind_read_complete"
            attempt.evaluated_source_revision = snapshot.source_revision
            attempt.evidence_json = dict(snapshot.evidence_refs)
            await session.flush()
            return row

    async def _compare(
        self,
        snapshot: EvaluationSnapshot,
        reading_row: MemoryIndependentReadingRow,
        reading: IndependentReading,
    ) -> ComparisonVerdict:
        instructions = (
            "Compare a foreground memory proposal with an already-committed blind reading. "
            "The blind reading is immutable and must not be revised. Emit only an epistemic "
            "delta: agree, narrow_scope, contradicted, insufficient_evidence, or "
            "different_category. normalized_content must contain only what the blind reading "
            "supports; category/scope/durability may narrow or correct the proposal but are not "
            "authority decisions. Return exactly one JSON object with keys verdict, "
            "normalized_content, category, scope, durability. Use only these exact values: verdict "
            "is agree, narrow_scope, contradicted, insufficient_evidence, or different_category; "
            "scope is chat, project, cross_chat, or null; durability is short_term, long_term, or "
            "null. Any other verdict fails the comparison."
        )
        payload = {
            "independent_reading_id": str(reading_row.id),
            "independent_reading": reading.model_dump(mode="json"),
            "proposal": snapshot.candidate,
        }
        raw = await self.model.complete_text(
            instructions=instructions,
            messages=[{
                "role": "user",
                "content": json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    default=str,
                ),
            }],
        )
        payload, notes = normalize_comparison_payload(
            self._json_object(raw, "proposal comparison")
        )
        self._note_contract("comparison", notes)
        verdict = ComparisonVerdict.model_validate(payload)
        if (
            verdict.verdict not in {"contradicted", "insufficient_evidence"}
            and not (verdict.normalized_content or "").strip()
        ):
            raise ValueError("supported comparison requires normalized_content")
        return verdict

    async def _persist_comparison(
        self,
        claim: CandidateLease,
        reading_row: MemoryIndependentReadingRow,
        comparison: ComparisonVerdict,
    ) -> MemoryComparisonVerdictRow:
        async with self.factory() as session, session.begin():
            candidate, attempt = await self._locked_live_claim(session, claim)
            reading = await session.get(MemoryIndependentReadingRow, reading_row.id)
            if reading is None or reading.tombstoned_at is not None:
                raise CandidateSnapshotError("blind reading disappeared before comparison")
            existing = (
                await session.execute(
                    select(MemoryComparisonVerdictRow).where(
                        MemoryComparisonVerdictRow.reading_id == reading.id
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                return existing
            _advance_candidate_state(candidate, attempt)
            row = MemoryComparisonVerdictRow(
                candidate_id=candidate.id,
                reading_id=reading.id,
                verdict=comparison.verdict,
                normalized_content=(
                    " ".join(comparison.normalized_content.split()).strip()
                    if comparison.normalized_content
                    else None
                ),
                category=comparison.category,
                scope=comparison.scope,
                durability=comparison.durability,
            )
            session.add(row)
            attempt.status = "epistemically_evaluated"
            await session.flush()
            return row

    async def _reusable_verification(
        self, snapshot: EvaluationSnapshot
    ) -> tuple[
        MemoryIndependentReadingRow,
        IndependentReading,
        MemoryComparisonVerdictRow,
        ComparisonVerdict,
    ] | None:
        evidence_hash = str(snapshot.evidence_refs.get("evidence_set_hash") or "")
        if not evidence_hash:
            return None
        async with self.factory() as session:
            row = (
                await session.execute(
                    select(MemoryComparisonVerdictRow, MemoryIndependentReadingRow)
                    .join(
                        MemoryIndependentReadingRow,
                        MemoryIndependentReadingRow.id
                        == MemoryComparisonVerdictRow.reading_id,
                    )
                    .where(
                        MemoryComparisonVerdictRow.candidate_id == snapshot.candidate_id,
                        MemoryComparisonVerdictRow.tombstoned_at.is_(None),
                        MemoryIndependentReadingRow.tombstoned_at.is_(None),
                        MemoryIndependentReadingRow.evidence_set_hash == evidence_hash,
                        MemoryIndependentReadingRow.source_revision == snapshot.source_revision,
                    )
                    .order_by(MemoryComparisonVerdictRow.created_at.desc())
                    .limit(1)
                )
            ).first()
            if row is None:
                return None
            comparison_row, reading_row = row
            reading = IndependentReading(
                extracted_claims=list(reading_row.extracted_claims_json or []),
                category=reading_row.category,
                scope=reading_row.scope,
                durability=reading_row.durability,
                claim_principal=reading_row.claim_principal,
                event_valid_from=reading_row.event_valid_from,
                event_valid_to=reading_row.event_valid_to,
            )
            comparison = ComparisonVerdict(
                verdict=comparison_row.verdict,
                normalized_content=comparison_row.normalized_content,
                category=comparison_row.category,
                scope=comparison_row.scope,
                durability=comparison_row.durability,
            )
            return reading_row, reading, comparison_row, comparison

    async def _reconcile_policy_publish(
        self,
        claim: CandidateLease,
        snapshot: EvaluationSnapshot,
        reading_row: MemoryIndependentReadingRow,
        reading: IndependentReading,
        comparison_row: MemoryComparisonVerdictRow,
        comparison: ComparisonVerdict,
    ) -> tuple[str, int]:
        version_conflicts = 0
        current = await self._refresh_memory_snapshot(
            claim, snapshot, reading, comparison
        )
        for _ in range(3):
            relation = await self._reconcile(current, reading, comparison)
            relation = self._enforce_temporal_order(current, reading, relation)
            reconciliation_row = await self._persist_reconciliation(
                claim,
                comparison_row,
                current,
                relation,
            )
            policy, reason = self._owner_policy(current, reading, comparison, relation)
            await self._persist_policy(
                claim, reconciliation_row, policy=policy, reason=reason
            )
            if policy == "block":
                await self._finish_policy_block(claim, reason or "policy_blocked")
                return "blocked", version_conflicts

            publication_snapshot = replace(
                current,
                publication_allowed=(
                    self._effective_scope(current, reading, comparison)
                    in {"chat", "cross_chat"}
                ),
                evidence_refs={
                    **current.evidence_refs,
                    "reconciliation_record_id": str(reconciliation_row.id),
                },
            )
            decision = self._publication_decision(
                publication_snapshot,
                reading,
                comparison,
                relation,
                policy=policy,
            )
            try:
                receipt = await self.publisher.publish(
                    claim, publication_snapshot, decision
                )
            except DBAPIError as exc:
                if not _is_retryable_serialization_error(exc):
                    raise
                version_conflicts += 1
                current = await self._refresh_memory_snapshot(
                    claim, current, reading, comparison
                )
                continue
            if receipt.outcome != "version_conflict":
                result = str(receipt.result.get("result") or "")
                if result == "source_conflict":
                    await self._release_after_conflict(
                        claim, code="source_conflict"
                    )
                return result, version_conflicts

            version_conflicts += 1
            current = await self._refresh_memory_snapshot(
                claim, current, reading, comparison
            )

        # Keep the blind reading/comparison reusable; only memory reconciliation was stale.
        await self._requeue_after_version_conflict(claim, current)
        return "version_conflict", version_conflicts

    async def _refresh_memory_snapshot(
        self,
        claim: CandidateLease,
        snapshot: EvaluationSnapshot,
        reading: IndependentReading,
        comparison: ComparisonVerdict,
    ) -> EvaluationSnapshot:
        async with self.factory() as session, session.begin():
            await session.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            )
            candidate, _attempt = await self._locked_live_claim(session, claim)
            transcript = await session.get(TranscriptRow, candidate.source_transcript_id)
            if (
                transcript is None
                or int(transcript.content_revision or 0) != snapshot.source_revision
                or not await MemoryCandidateLeaseRepository._evidence_live(
                    session, candidate
                )
            ):
                raise CandidateSnapshotError(
                    "canonical evidence changed during reconciliation"
                )
            scope = self._effective_scope(snapshot, reading, comparison)
            scope_key = (
                "owner"
                if scope == "cross_chat"
                else (
                    f"chat:{candidate.source_transcript_id}"
                    if scope == "chat"
                    else f"project-unresolved:{candidate.source_transcript_id}"
                )
            )
            kind = self._effective_kind(snapshot, reading, comparison)
            explicit_target_raw = snapshot.candidate.get("explicit_target_memory_id")
            explicit_target_id = (
                UUID(str(explicit_target_raw)) if explicit_target_raw else None
            )
            active, restricted, legacy = await self._related_memories_for(
                session,
                candidate,
                kind=kind,
                scope=scope,
                scope_key=scope_key,
                explicit_target_memory_id=explicit_target_id,
            )
            revision = await _memory_revision(session)
            return replace(
                snapshot,
                memory_revision=revision,
                active_memories=[_project_for_model(row) for row in active],
                restricted_memories=[
                    _project_for_model(row) for row in restricted
                ],
                legacy_unverified_targets=[
                    _project_for_model(row) for row in legacy
                ],
                evidence_refs={
                    **snapshot.evidence_refs,
                    "active_memory_ids": [str(row.id) for row in active],
                    "restricted_memory_ids": [str(row.id) for row in restricted],
                    "legacy_unverified_target_ids": [str(row.id) for row in legacy],
                },
            )

    async def _reconcile(
        self,
        snapshot: EvaluationSnapshot,
        reading: IndependentReading,
        comparison: ComparisonVerdict,
    ) -> MemoryRelationDecision:
        content = self._effective_content(snapshot, reading, comparison)
        instructions = (
            "Reconcile one independently verified claim against the current Atlas memory graph. "
            "The active/restricted memories are derived artifacts for state comparison, not "
            "corroborating evidence for the claim. Legacy-unverified memories are target-only "
            "artifacts: they must never count as evidence for the claim. Choose exactly one "
            "relation: new, duplicate_of, narrows, supersedes, historical_predecessor, "
            "grounds_legacy, or conflicts_with. Semantic identity is about the proposition, not "
            "its classification. Two memories that assert materially the same proposition are "
            "duplicate_of even when their kind, scope, durability, subject, namespace, or wording "
            "differ; those fields classify or constrain a claim and do not by themselves make a "
            "new proposition. Use narrows only when the verified claim adds a genuine semantic "
            "restriction or additional supported detail to an active memory. Use new only when the "
            "claim contributes materially distinct information. Temporal qualifiers remain "
            "semantically significant: a claim about a different time or a later state is not a "
            "duplicate of an earlier one. grounds_legacy is only for a supplied "
            "legacy_unverified_target whose meaning is supported by the verified claim; its "
            "replacement_content must be the verified claim wording, not the legacy wording. "
            "Other targeted relations must name one supplied active memory_id. Never narrow "
            "or supersede owner-directed memory; if the claim disagrees with owner-directed "
            "memory use conflicts_with. Explicit command intent, when present, is structural owner "
            "intent rather than corroborating evidence. For an explicit correction, compare the "
            "verified claim against its supplied target. If that target is an active derived memory, "
            "a supported replacement should normally supersede it. If the target is only a lifecycle-"
            "restricted correction guard, return new when the claim is otherwise new; publication "
            "will preserve the historical link. Do not use discovery/write order as chronology. "
            "Return exactly one JSON object with keys relation, target_memory_id, replacement_content. "
            "relation must be exactly one of the seven values above; any other relation fails."
        )
        payload = {
            "verified_claim": {
                "content": content,
                "category": self._effective_kind(snapshot, reading, comparison),
                "scope": self._effective_scope(snapshot, reading, comparison),
                "durability": self._effective_durability(snapshot, reading, comparison),
                "event_valid_from": reading.event_valid_from,
                "event_valid_to": reading.event_valid_to,
            },
            "applicable_active_memories": snapshot.active_memories,
            "lifecycle_restricted_memories": snapshot.restricted_memories,
            "legacy_unverified_targets": snapshot.legacy_unverified_targets,
            "explicit_command_intent": {
                "operation": snapshot.candidate.get("explicit_operation"),
                "target_memory_id": snapshot.candidate.get("explicit_target_memory_id"),
                "change_type": snapshot.candidate.get("explicit_change_type"),
            },
        }
        raw = await self.model.complete_text(
            instructions=instructions,
            messages=[{
                "role": "user",
                "content": json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    default=str,
                ),
            }],
        )
        payload, _notes = normalize_relation_payload(
            self._json_object(raw, "memory reconciliation")
        )
        relation = MemoryRelationDecision.model_validate(payload)
        active_ids = {
            UUID(str(item["memory_id"])) for item in snapshot.active_memories
        }
        legacy_ids = {
            UUID(str(item["memory_id"])) for item in snapshot.legacy_unverified_targets
        }
        if relation.relation == "grounds_legacy":
            if relation.target_memory_id not in legacy_ids:
                raise ValueError("legacy grounding targeted memory outside target-only graph")
            if not (relation.replacement_content or "").strip():
                raise ValueError("legacy grounding requires verified replacement content")
        elif relation.target_memory_id is not None and relation.target_memory_id not in active_ids:
            raise ValueError("reconciliation targeted memory outside evaluated active graph")
        return relation

    @staticmethod
    def _enforce_temporal_order(
        snapshot: EvaluationSnapshot,
        reading: IndependentReading,
        relation: MemoryRelationDecision,
    ) -> MemoryRelationDecision:
        if relation.relation not in {"supersedes", "historical_predecessor"}:
            return relation
        target = next(
            (
                item for item in snapshot.active_memories
                if item.get("memory_id") == str(relation.target_memory_id)
            ),
            None,
        )
        if target is None:
            raise ValueError("temporal target disappeared from evaluated graph")
        # An explicit memory.correct naming this exact target is structural owner
        # intent: it may supersede an owner-directed row and needs no event ordering.
        # The evidence-horizon guard still applies; a derived candidate still conflicts.
        explicit_correction = (
            str(snapshot.candidate.get("explicit_operation") or "") == "correct"
            and str(snapshot.candidate.get("explicit_target_memory_id") or "")
            == str(relation.target_memory_id)
        )
        if target.get("authority") == "owner_directed" and not explicit_correction:
            return relation.model_copy(update={"relation": "conflicts_with"})
        candidate_time = reading.event_valid_from
        evidence_times: list[datetime] = []
        for item in snapshot.evidence_items:
            observed = item.get("recorded_at")
            if isinstance(observed, str):
                try:
                    observed = datetime.fromisoformat(observed)
                except ValueError:
                    observed = None
            if isinstance(observed, datetime):
                evidence_times.append(observed)
        cited_horizon = max(evidence_times) if evidence_times else None
        sweep_horizon = snapshot.candidate.get("temporal_horizon_at")
        if isinstance(sweep_horizon, str):
            try:
                sweep_horizon = datetime.fromisoformat(sweep_horizon)
            except ValueError:
                sweep_horizon = None
        if snapshot.candidate.get("intake_path") == "sweep" and isinstance(
            sweep_horizon, datetime
        ):
            evidence_horizon = (
                min(cited_horizon, sweep_horizon)
                if cited_horizon is not None
                else sweep_horizon
            )
        else:
            evidence_horizon = cited_horizon
        if (
            candidate_time is not None
            and evidence_horizon is not None
            and candidate_time > evidence_horizon
        ):
            return relation.model_copy(update={
                "relation": "conflicts_with",
                "temporal_guard": "beyond_evidence_horizon",
            })
        if explicit_correction and relation.relation == "supersedes":
            return relation.model_copy(update={
                "relation": "supersedes",
                "temporal_guard": "explicit_owner_correction",
            })
        target_time = target.get("event_valid_from")
        if isinstance(target_time, str):
            try:
                target_time = datetime.fromisoformat(target_time)
            except ValueError:
                target_time = None
        if candidate_time is None or not isinstance(target_time, datetime):
            return relation.model_copy(update={
                "relation": "conflicts_with",
                "temporal_guard": "event_order_unknown",
            })
        if candidate_time > target_time:
            return relation.model_copy(update={
                "relation": "supersedes",
                "temporal_guard": "event_order_checked",
            })
        if candidate_time < target_time:
            return relation.model_copy(update={
                "relation": "historical_predecessor",
                "temporal_guard": "event_order_checked",
            })
        return relation.model_copy(update={
            "relation": "conflicts_with",
            "temporal_guard": "event_times_equal",
        })

    async def _persist_reconciliation(
        self,
        claim: CandidateLease,
        comparison_row: MemoryComparisonVerdictRow,
        snapshot: EvaluationSnapshot,
        relation: MemoryRelationDecision,
    ) -> MemoryReconciliationRecordRow:
        async with self.factory() as session, session.begin():
            candidate, attempt = await self._locked_live_claim(session, claim)
            comparison = await session.get(
                MemoryComparisonVerdictRow, comparison_row.id
            )
            if comparison is None or comparison.tombstoned_at is not None:
                raise CandidateSnapshotError("comparison disappeared before reconciliation")
            _advance_candidate_state(candidate, attempt)
            row = MemoryReconciliationRecordRow(
                candidate_id=candidate.id,
                comparison_id=comparison.id,
                relation=relation.relation,
                target_memory_id=relation.target_memory_id,
                evaluated_memory_revision=snapshot.memory_revision,
                replacement_content=(
                    " ".join(relation.replacement_content.split()).strip()
                    if relation.replacement_content
                    else None
                ),
                temporal_guard=(
                    relation.temporal_guard
                    or (
                        "event_order_checked"
                        if relation.relation
                        in {"supersedes", "historical_predecessor", "conflicts_with"}
                        and relation.target_memory_id is not None
                        else None
                    )
                ),
            )
            session.add(row)
            attempt.status = "reconciled"
            attempt.evaluated_memory_revision = snapshot.memory_revision
            attempt.semantic_decision = relation.relation
            attempt.target_memory_id = relation.target_memory_id
            await session.flush()
            return row

    def _owner_policy(
        self,
        snapshot: EvaluationSnapshot,
        reading: IndependentReading,
        comparison: ComparisonVerdict,
        relation: MemoryRelationDecision,
    ) -> tuple[str, str | None]:
        category = self._policy_category(snapshot, reading, comparison)
        scope = self._effective_scope(snapshot, reading, comparison)
        if scope == "project":
            return "block", "project_identity_unresolved"
        origin = str(snapshot.candidate.get("origin") or "conversation")
        explicit_intent = origin in {"explicit_owner_request", "explicit_owner_correction"}
        if explicit_intent:
            # Storage intent is not evidence authority. An explicit request may waive
            # the redundant confirmation step only when the blind reader says the
            # supported claim itself is owner-principal. External/mixed claims still
            # require review, and earlier hard policy blocks remain authoritative.
            if reading.claim_principal == "owner":
                return "allow", "explicit_owner_storage_intent"
            if bool(snapshot.candidate.get("owner_confirmation_granted")):
                return "allow", "owner_confirmed_external_claim"
            return "require_owner_confirmation", "explicit_intent_external_claim"
        if origin not in {"conversation", "owner_statement", "sweep"}:
            if bool(snapshot.candidate.get("owner_confirmation_granted")):
                return "allow", "owner_confirmed_import"
            return "require_owner_confirmation", "external_origin_requires_review"
        if category.casefold() in self._SENSITIVE_CATEGORIES:
            if bool(snapshot.candidate.get("owner_confirmation_granted")):
                return "allow", "owner_confirmed"
            return "require_owner_confirmation", "sensitive_category"
        if relation.relation == "conflicts_with":
            return "allow", None
        return "allow", None

    async def _persist_policy(
        self,
        claim: CandidateLease,
        reconciliation_row: MemoryReconciliationRecordRow,
        *,
        policy: str,
        reason: str | None,
    ) -> MemoryPolicyDecisionRow:
        async with self.factory() as session, session.begin():
            candidate, attempt = await self._locked_live_claim(session, claim)
            existing = (
                await session.execute(
                    select(MemoryPolicyDecisionRow).where(
                        MemoryPolicyDecisionRow.reconciliation_id
                        == reconciliation_row.id
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                return existing
            _advance_candidate_state(candidate, attempt)
            row = MemoryPolicyDecisionRow(
                candidate_id=candidate.id,
                reconciliation_id=reconciliation_row.id,
                decision=policy,
                reason_code=reason,
            )
            session.add(row)
            attempt.status = (
                "awaiting_policy_confirmation"
                if policy == "require_owner_confirmation"
                else "policy_allowed" if policy == "allow" else "policy_blocked"
            )
            await session.flush()
            return row

    def _publication_decision(
        self,
        snapshot: EvaluationSnapshot,
        reading: IndependentReading,
        comparison: ComparisonVerdict,
        relation: MemoryRelationDecision,
        *,
        policy: str,
    ) -> ReconciliationDecision:
        content = relation.replacement_content or self._effective_content(
            snapshot, reading, comparison
        )
        kind = self._effective_kind(snapshot, reading, comparison)
        scope = self._effective_scope(snapshot, reading, comparison)
        durability = self._effective_durability(snapshot, reading, comparison)
        if policy == "require_owner_confirmation":
            action = "await_owner"
            target = None
        elif durability == "short_term" and relation.relation != "duplicate_of":
            action = "retain_short_term"
            target = None
        else:
            action, target = {
                "new": ("create", None),
                "duplicate_of": ("equivalent", relation.target_memory_id),
                "narrows": ("merge", relation.target_memory_id),
                "supersedes": ("supersede", relation.target_memory_id),
                "historical_predecessor": (
                    "historical_predecessor", relation.target_memory_id
                ),
                "grounds_legacy": ("ground_legacy", relation.target_memory_id),
                "conflicts_with": ("conflict", relation.target_memory_id),
            }[relation.relation]
        return ReconciliationDecision(
            decision=action,
            target_memory_id=target,
            content=(
                content
                if action
                in {"merge", "supersede", "historical_predecessor", "ground_legacy", "conflict"}
                else None
            ),
            event_valid_from=reading.event_valid_from,
            event_valid_to=reading.event_valid_to,
            memory_kind=kind,
            scope=scope,
            durability=durability,
            reason=(relation.temporal_guard if action == "conflict" else None),
        )

    async def _finish_epistemic_rejection(
        self, claim: CandidateLease, *, code: str
    ) -> None:
        async with self.factory() as session, session.begin():
            candidate, attempt = await self._locked_live_claim(session, claim)
            now = utcnow()
            _advance_candidate_state(candidate, attempt)
            candidate.status = "discarded"
            candidate.lease_token = None
            candidate.leased_until = None
            candidate.processed_at = now
            candidate.decision_json = {
                "decision": "discard",
                "code": code,
            }
            attempt.status = "committed"
            attempt.completed_at = now
            attempt.semantic_decision = "discard"
            attempt.result_json = {"result": "discarded", "code": code}
            await settle_explicit_obligation(session, candidate, now=now, code=code)

    async def _finish_policy_block(
        self, claim: CandidateLease, code: str
    ) -> None:
        async with self.factory() as session, session.begin():
            candidate, attempt = await self._locked_live_claim(session, claim)
            now = utcnow()
            _advance_candidate_state(candidate, attempt)
            candidate.status = "blocked"
            candidate.lease_token = None
            candidate.leased_until = None
            candidate.processed_at = now
            candidate.decision_json = {"decision": "blocked", "code": code}
            attempt.status = "committed"
            attempt.completed_at = now
            attempt.result_json = {"result": "blocked", "code": code}
            await settle_explicit_obligation(session, candidate, now=now, code=code)

    async def _requeue_after_version_conflict(
        self, claim: CandidateLease, snapshot: EvaluationSnapshot
    ) -> None:
        async with self.factory() as session, session.begin():
            candidate = (
                await session.execute(
                    select(MemoryCandidateRow)
                    .where(MemoryCandidateRow.id == claim.candidate_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            attempt = (
                await session.execute(
                    select(MemoryReconciliationAttemptRow)
                    .where(MemoryReconciliationAttemptRow.id == claim.attempt_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            now = utcnow()
            if (
                candidate is not None
                and attempt is not None
                and attempt.completed_at is None
                and candidate.status == "leased"
                and candidate.lease_token == claim.lease_token
                and int(candidate.state_version or 0)
                == int(attempt.candidate_state_version or 0)
            ):
                _advance_candidate_state(candidate, attempt)
                attempt.status = "version_conflict"
                attempt.completed_at = now
                attempt.result_json = {
                    "result": "version_conflict",
                    "expected_revision": snapshot.memory_revision,
                }
                candidate.status = "pending"
                candidate.lease_token = None
                candidate.leased_until = None
                candidate.decision_json = {
                    "decision": "reconcile_retry_needed",
                    "code": "version_conflict",
                }

    async def _release_after_conflict(
        self, claim: CandidateLease, *, code: str
    ) -> None:
        async with self.factory() as session, session.begin():
            candidate = (
                await session.execute(
                    select(MemoryCandidateRow)
                    .where(MemoryCandidateRow.id == claim.candidate_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            attempt = (
                await session.execute(
                    select(MemoryReconciliationAttemptRow)
                    .where(MemoryReconciliationAttemptRow.id == claim.attempt_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if (
                candidate is not None
                and attempt is not None
                and attempt.completed_at is None
                and candidate.status == "leased"
                and candidate.lease_token == claim.lease_token
                and int(candidate.state_version or 0)
                == int(attempt.candidate_state_version or 0)
            ):
                _advance_candidate_state(candidate, attempt)
                attempt.status = code
                attempt.completed_at = utcnow()
                attempt.result_json = {"result": code}
                await self._requeue_or_fail(session, candidate)

    async def _release_failed(
        self, claim: CandidateLease, *, code: str
    ) -> None:
        async with self.factory() as session, session.begin():
            candidate = await session.get(MemoryCandidateRow, claim.candidate_id)
            attempt = await session.get(
                MemoryReconciliationAttemptRow, claim.attempt_id
            )
            if (
                candidate is None
                or attempt is None
                or attempt.completed_at is not None
                or candidate.status in _TERMINAL_CANDIDATE_STATUSES
                or candidate.status != "leased"
                or candidate.lease_token != claim.lease_token
                or int(candidate.state_version or 0)
                != int(attempt.candidate_state_version or 0)
            ):
                return
            _advance_candidate_state(candidate, attempt)
            attempt.status = "failed"
            attempt.error = code
            attempt.completed_at = utcnow()
            attempt.result_json = {"result": "failed", "code": code}
            await self._requeue_or_fail(session, candidate, code=code)

    async def _requeue_or_fail(
        self,
        session: AsyncSession,
        candidate: MemoryCandidateRow,
        *,
        code: str | None = None,
    ) -> None:
        candidate.lease_token = None
        candidate.leased_until = None
        if int(candidate.attempt_count or 0) >= self.max_attempts:
            now = utcnow()
            candidate.status = "failed"
            candidate.processed_at = now
            candidate.decision_json = {
                "decision": "failed",
                "code": "attempt_limit",
                **({"last_error": code} if code else {}),
            }
            await settle_explicit_obligation(
                session, candidate, now=now, code=(code or "attempt_limit")
            )
        else:
            # Automatic retry: the candidate returns to the queue and any
            # explicit owner obligation stays pending untouched.
            candidate.status = "pending"

    @staticmethod
    async def _locked_live_claim(
        session: AsyncSession, claim: CandidateLease
    ) -> tuple[MemoryCandidateRow, MemoryReconciliationAttemptRow]:
        candidate = (
            await session.execute(
                select(MemoryCandidateRow)
                .where(MemoryCandidateRow.id == claim.candidate_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        attempt = (
            await session.execute(
                select(MemoryReconciliationAttemptRow)
                .where(MemoryReconciliationAttemptRow.id == claim.attempt_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        now = utcnow()
        if (
            candidate is None
            or attempt is None
            or candidate.status != "leased"
            or candidate.lease_token != claim.lease_token
            or attempt.lease_token != claim.lease_token
            or attempt.completed_at is not None
            or int(candidate.state_version or 0) != int(attempt.candidate_state_version or 0)
            or candidate.leased_until is None
            or candidate.leased_until <= now
            or candidate.invalidated_at is not None
            or not await MemoryCandidateLeaseRepository._evidence_live(
                session, candidate
            )
        ):
            raise CandidateSnapshotError("candidate lease/evidence is no longer live")
        return candidate, attempt

    @staticmethod
    def _json_object(raw: str, stage: str) -> dict[str, object]:
        value = raw.strip()
        if not (value.startswith("{") and value.endswith("}")):
            raise ValueError(f"{stage} model did not return one JSON object")
        parsed = json.loads(value)
        if not isinstance(parsed, dict):
            raise TypeError(f"{stage} model did not return one JSON object")
        return parsed

    @staticmethod
    def _effective_content(
        snapshot: EvaluationSnapshot,
        reading: IndependentReading,
        comparison: ComparisonVerdict,
    ) -> str:
        value = comparison.normalized_content
        if not value and reading.extracted_claims:
            value = reading.extracted_claims[0]
        if not value:
            value = str(snapshot.candidate.get("content") or "")
        return clean_memory_content(value)

    @staticmethod
    def _policy_category(
        snapshot: EvaluationSnapshot,
        reading: IndependentReading,
        comparison: ComparisonVerdict,
    ) -> str:
        return str(
            comparison.category
            or reading.category
            or snapshot.candidate.get("kind")
            or "fact"
        ).strip()

    @classmethod
    def _effective_kind(
        cls,
        snapshot: EvaluationSnapshot,
        reading: IndependentReading,
        comparison: ComparisonVerdict,
    ) -> str:
        value = cls._policy_category(snapshot, reading, comparison)
        if value in cls._SUPPORTED_KINDS:
            return value
        proposed = str(snapshot.candidate.get("kind") or "fact").strip()
        return proposed if proposed in cls._SUPPORTED_KINDS else "fact"

    @staticmethod
    def _effective_scope(
        snapshot: EvaluationSnapshot,
        reading: IndependentReading,
        comparison: ComparisonVerdict,
    ) -> str:
        return str(
            comparison.scope
            or reading.scope
            or snapshot.candidate.get("scope")
            or "chat"
        )

    @staticmethod
    def _effective_durability(
        snapshot: EvaluationSnapshot,
        reading: IndependentReading,
        comparison: ComparisonVerdict,
    ) -> str:
        return str(
            comparison.durability
            or reading.durability
            or snapshot.candidate.get("durability")
            or "short_term"
        )

