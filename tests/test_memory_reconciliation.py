from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from atlas.memory.candidates import MemoryCandidateIntake
from atlas.memory.continuity import ContinuityCapsuleService
from atlas.memory.durable import RETIRED, DurableMemoryRepository
from atlas.memory.embeddings import (
    DurableMemoryEmbeddingIndexer,
    TranscriptEmbeddingIndexer,
)
from atlas.memory.lifecycle import MemoryLifecycleCommands
from atlas.memory.reconciliation import (
    DerivedMemoryPublisher,
    MemoryCandidateLeaseRepository,
    MemoryReconciliationService,
    ReconciliationDecision,
)
from atlas.persistence.models import (
    ContinuityCapsuleRow,
    DurableMemoryRow,
    MemoryCandidateRow,
    MemoryProvenanceRow,
    MemoryReconciliationAttemptRow,
    SharedResourceVersionRow,
    TranscriptIndexChunkRow,
    TranscriptRow,
    TurnRow,
)
from sqlalchemy import func, select, update


class _StaticModel:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    async def complete_text(self, *, instructions: str, messages: list[dict[str, str]]) -> str:
        return json.dumps(self.payload)


class _PipelineModel:
    model = "test-pipeline"

    def __init__(
        self,
        *,
        content: str,
        kind: str = "preference",
        scope: str = "cross_chat",
        durability: str = "long_term",
        relation: str = "new",
        target_memory_id: UUID | None = None,
    ) -> None:
        self.content = content
        self.kind = kind
        self.scope = scope
        self.durability = durability
        self.relation = relation
        self.target_memory_id = target_memory_id

    async def complete_text(
        self, *, instructions: str, messages: list[dict[str, str]]
    ) -> str:
        if "blind evidence-reading stage" in instructions:
            return json.dumps({
                "extracted_claims": [self.content],
                "category": self.kind,
                "scope": self.scope,
                "durability": self.durability,
                "event_valid_from": None,
                "event_valid_to": None,
            })
        if "Compare a foreground memory proposal" in instructions:
            return json.dumps({
                "verdict": "agree",
                "normalized_content": self.content,
                "category": self.kind,
                "scope": self.scope,
                "durability": self.durability,
            })
        if "Reconcile one independently verified claim" in instructions:
            return json.dumps({
                "relation": self.relation,
                "target_memory_id": (
                    str(self.target_memory_id) if self.target_memory_id else None
                ),
                "replacement_content": (
                    self.content if self.relation in {
                        "narrows", "supersedes", "historical_predecessor",
                        "conflicts_with",
                    } else None
                ),
            })
        raise AssertionError("unexpected verification stage")


async def _seed_candidate(
    pg_factory,
    *,
    content: str = "Jaco prefers fresh topic chats.",
    kind: str = "preference",
    scope: str = "cross_chat",
    durability: str = "long_term",
    subject: str | None = "Jaco",
):
    async with pg_factory() as session:
        transcript = TranscriptRow(kind="owner", next_turn_sequence=1, content_revision=1)
        session.add(transcript)
        await session.flush()
        turn = TurnRow(
            transcript_id=transcript.id,
            sequence=1,
            actor="owner",
            blocks=[{"type": "text", "text": content}],
        )
        session.add(turn)
        await session.commit()
        transcript_id, turn_id = transcript.id, turn.id

    raw = {
        "kind": kind,
        "content": content,
        "scope": scope,
        "confidence": 0.9,
        "durability": durability,
        "proposed_action": "upsert",
        "subject": subject,
        "evidence_refs": [{"turn_id": str(turn_id), "span_ref": "text:0"}],
    }
    result = await MemoryCandidateIntake(pg_factory).enqueue_many(
        [raw],
        source_transcript_id=transcript_id,
        source_turn_id=turn_id,
        source_provider_evidence_id=None,
    )
    assert result["accepted"] == 1
    async with pg_factory() as session:
        candidate = (
            await session.execute(
                select(MemoryCandidateRow).where(
                    MemoryCandidateRow.source_turn_id == turn_id,
                    MemoryCandidateRow.status == "pending",
                )
            )
        ).scalar_one()
        return candidate.id, transcript_id, turn_id, raw


async def _claim_and_snapshot(pg_factory, candidate_id: UUID, model=None):
    leases = MemoryCandidateLeaseRepository(pg_factory, lease_seconds=30, max_attempts=3)
    claims, _ = await leases.claim_batch(limit=8)
    claim = next(item for item in claims if item.candidate_id == candidate_id)
    service = MemoryReconciliationService(
        pg_factory,
        model or _StaticModel({"decision": "discard", "target_memory_id": None, "content": None, "reason": "test"}),
        lease_seconds=30,
        max_attempts=3,
    )
    snapshot = await service._load_snapshot(claim)
    return claim, snapshot, service


@pytest.mark.asyncio
async def test_stale_lease_holder_cannot_publish_when_memory_revision_is_unchanged(pg_factory):
    candidate_id, _, _, _ = await _seed_candidate(pg_factory)
    claim_a, snapshot, _ = await _claim_and_snapshot(pg_factory, candidate_id)

    async with pg_factory() as session:
        row = await session.get(MemoryCandidateRow, candidate_id)
        row.leased_until = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()

    claims_b, _ = await MemoryCandidateLeaseRepository(
        pg_factory, lease_seconds=30, max_attempts=3
    ).claim_batch(limit=8)
    claim_b = next(item for item in claims_b if item.candidate_id == candidate_id)
    assert claim_b.lease_token != claim_a.lease_token

    receipt = await DerivedMemoryPublisher(pg_factory).publish(
        claim_a,
        snapshot,
        ReconciliationDecision(decision="create"),
    )
    assert receipt.outcome == "no_change"
    assert receipt.result["result"] == "lease_conflict"

    async with pg_factory() as session:
        candidate = await session.get(MemoryCandidateRow, candidate_id)
        old_attempt = await session.get(MemoryReconciliationAttemptRow, claim_a.attempt_id)
        count = (await session.execute(select(func.count()).select_from(DurableMemoryRow))).scalar_one()
        assert candidate.lease_token == claim_b.lease_token
        assert old_attempt.status == "lease_expired"
        assert count == 0


@pytest.mark.asyncio
async def test_memory_revision_change_after_snapshot_rejects_publication(pg_factory):
    candidate_id, _, _, _ = await _seed_candidate(pg_factory)
    claim, snapshot, _ = await _claim_and_snapshot(pg_factory, candidate_id)
    async with pg_factory() as session:
        session.add(SharedResourceVersionRow(resource_type="memory_state", resource_id="owner", version=1))
        await session.commit()

    receipt = await DerivedMemoryPublisher(pg_factory).publish(
        claim, snapshot, ReconciliationDecision(decision="create")
    )
    assert receipt.outcome == "version_conflict"
    assert receipt.observed_version == 1
    async with pg_factory() as session:
        assert (await session.execute(select(func.count()).select_from(DurableMemoryRow))).scalar_one() == 0


@pytest.mark.asyncio
async def test_source_revision_change_after_snapshot_requeues_without_publication(pg_factory):
    candidate_id, transcript_id, _, _ = await _seed_candidate(pg_factory)
    claim, snapshot, _ = await _claim_and_snapshot(pg_factory, candidate_id)
    async with pg_factory() as session:
        await session.execute(
            update(TranscriptRow)
            .where(TranscriptRow.id == transcript_id)
            .values(content_revision=TranscriptRow.content_revision + 1)
        )
        await session.commit()

    receipt = await DerivedMemoryPublisher(pg_factory).publish(
        claim, snapshot, ReconciliationDecision(decision="create")
    )
    assert receipt.result["result"] == "source_conflict"
    async with pg_factory() as session:
        candidate = await session.get(MemoryCandidateRow, candidate_id)
        assert candidate.status == "pending"
        assert candidate.lease_token is None
        assert (await session.execute(select(func.count()).select_from(DurableMemoryRow))).scalar_one() == 0


@pytest.mark.asyncio
async def test_equivalent_adds_lineage_once_and_second_copy_is_true_no_change(pg_factory):
    candidate_id, transcript_id, turn_id, raw = await _seed_candidate(pg_factory)
    async with pg_factory() as session:
        memory, _ = await DurableMemoryRepository(session).create_active(
            raw["content"],
            source_transcript_id=None,
            source_turn_id=None,
            record_kind="owner_directed",
            memory_kind="preference",
            scope="cross_chat",
            scope_key="owner",
            durability="long_term",
            subject="Jaco",
        )
        await session.commit()
        memory_id = memory.id

    claim, snapshot, _ = await _claim_and_snapshot(pg_factory, candidate_id)
    first = await DerivedMemoryPublisher(pg_factory).publish(
        claim,
        snapshot,
        ReconciliationDecision(decision="equivalent", target_memory_id=memory_id),
    )
    assert first.outcome == "applied"
    assert first.result["result"] == "lineage_added"

    second_intake = await MemoryCandidateIntake(pg_factory).enqueue_many(
        [raw],
        source_transcript_id=transcript_id,
        source_turn_id=turn_id,
        source_provider_evidence_id=None,
    )
    assert second_intake["accepted"] == 1
    async with pg_factory() as session:
        second_candidate = (
            await session.execute(
                select(MemoryCandidateRow)
                .where(MemoryCandidateRow.status == "pending")
            )
        ).scalar_one()
    claim2, snapshot2, _ = await _claim_and_snapshot(pg_factory, second_candidate.id)
    second = await DerivedMemoryPublisher(pg_factory).publish(
        claim2,
        snapshot2,
        ReconciliationDecision(decision="equivalent", target_memory_id=memory_id),
    )
    assert second.outcome == "no_change"
    assert second.result["result"] == "already_represented"
    assert second.committed_version == first.committed_version

    async with pg_factory() as session:
        lineage_count = (
            await session.execute(
                select(func.count()).select_from(MemoryProvenanceRow).where(
                    MemoryProvenanceRow.memory_id == memory_id,
                    MemoryProvenanceRow.source_turn_id == turn_id,
                )
            )
        ).scalar_one()
        assert lineage_count == 1


@pytest.mark.asyncio
async def test_derived_publication_cannot_supersede_owner_directed_memory(pg_factory):
    candidate_id, _, _, _ = await _seed_candidate(
        pg_factory, content="Jaco likes concise reports.", subject="Jaco"
    )
    async with pg_factory() as session:
        target, _ = await DurableMemoryRepository(session).create_active(
            "Jaco likes detailed reports.",
            source_transcript_id=None,
            source_turn_id=None,
            record_kind="owner_directed",
            memory_kind="preference",
            scope="cross_chat",
            scope_key="owner",
            durability="long_term",
            subject="Jaco",
        )
        await session.commit()
        target_id = target.id

    claim, snapshot, _ = await _claim_and_snapshot(pg_factory, candidate_id)
    assert str(target_id) in {item["memory_id"] for item in snapshot.active_memories}
    receipt = await DerivedMemoryPublisher(pg_factory).publish(
        claim,
        snapshot,
        ReconciliationDecision(
            decision="supersede",
            target_memory_id=target_id,
            content="Jaco likes concise reports.",
        ),
    )
    assert receipt.outcome == "no_change"
    assert receipt.result["code"] == "derived_target_invalid"
    async with pg_factory() as session:
        target = await session.get(DurableMemoryRow, target_id)
        assert target.status == "active"
        assert target.content == "Jaco likes detailed reports."


@pytest.mark.asyncio
async def test_retired_owner_claim_cannot_be_resurrected_from_same_candidate(pg_factory):
    content = "Jaco prefers a specific temporary dashboard layout."
    candidate_id, _, _, _ = await _seed_candidate(pg_factory, content=content)
    async with pg_factory() as session:
        guard, _ = await DurableMemoryRepository(session).create_guard(
            content,
            status=RETIRED,
            record_kind="owner_retire_guard",
            source_transcript_id=None,
            source_turn_id=None,
        )
        await session.commit()
        guard_id = guard.id

    claim, snapshot, _ = await _claim_and_snapshot(pg_factory, candidate_id)
    assert str(guard_id) in {item["memory_id"] for item in snapshot.restricted_memories}
    receipt = await DerivedMemoryPublisher(pg_factory).publish(
        claim, snapshot, ReconciliationDecision(decision="create")
    )
    assert receipt.outcome == "no_change"
    assert receipt.result["code"] == "owner_lifecycle_restriction"
    async with pg_factory() as session:
        active_count = (
            await session.execute(
                select(func.count()).select_from(DurableMemoryRow).where(DurableMemoryRow.status == "active")
            )
        ).scalar_one()
        assert active_count == 0


@pytest.mark.asyncio
async def test_short_term_retention_is_not_durable_memory_and_remains_deduplicated(pg_factory):
    candidate_id, transcript_id, turn_id, raw = await _seed_candidate(
        pg_factory,
        content="Jaco is testing a temporary UI this afternoon.",
        kind="project_state",
        durability="short_term",
    )
    claim, snapshot, _ = await _claim_and_snapshot(pg_factory, candidate_id)
    receipt = await DerivedMemoryPublisher(
        pg_factory, short_term_review_hours=24, short_term_expiry_days=14
    ).publish(claim, snapshot, ReconciliationDecision(decision="retain_short_term"))
    assert receipt.outcome == "no_change"
    assert receipt.result["result"] == "retained_short_term"

    duplicate = await MemoryCandidateIntake(pg_factory).enqueue_many(
        [raw],
        source_transcript_id=transcript_id,
        source_turn_id=turn_id,
        source_provider_evidence_id=None,
    )
    assert duplicate["duplicate"] == 1
    async with pg_factory() as session:
        candidate = await session.get(MemoryCandidateRow, candidate_id)
        assert candidate.status == "retained_short_term"
        assert candidate.review_after is not None
        assert candidate.expires_at is not None
        assert (await session.execute(select(func.count()).select_from(DurableMemoryRow))).scalar_one() == 0


@pytest.mark.asyncio
async def test_identical_publication_retry_replays_same_operation_without_duplicate_memory(pg_factory):
    candidate_id, _, _, _ = await _seed_candidate(pg_factory)
    claim, snapshot, _ = await _claim_and_snapshot(pg_factory, candidate_id)
    operation_id = uuid4()
    publisher = DerivedMemoryPublisher(pg_factory)
    first = await publisher.publish(
        claim, snapshot, ReconciliationDecision(decision="create"), operation_id=operation_id
    )
    second = await publisher.publish(
        claim, snapshot, ReconciliationDecision(decision="create"), operation_id=operation_id
    )
    assert first.outcome == "applied"
    assert second.replayed is True
    assert second.operation_id == first.operation_id
    assert second.committed_version == first.committed_version
    async with pg_factory() as session:
        assert (await session.execute(select(func.count()).select_from(DurableMemoryRow))).scalar_one() == 1


@pytest.mark.asyncio
async def test_model_cannot_target_memory_that_was_not_in_evaluation_snapshot(pg_factory):
    candidate_id, _, _, _ = await _seed_candidate(pg_factory)
    claim, snapshot, _ = await _claim_and_snapshot(pg_factory, candidate_id)
    async with pg_factory() as session:
        target, _ = await DurableMemoryRepository(session).create_active(
            "A later derived memory the model never evaluated.",
            source_transcript_id=None,
            source_turn_id=None,
            record_kind="derived",
            memory_kind="preference",
            scope="cross_chat",
            scope_key="owner",
            durability="long_term",
            subject="Jaco",
        )
        await session.commit()
        target_id = target.id
    receipt = await DerivedMemoryPublisher(pg_factory).publish(
        claim,
        snapshot,
        ReconciliationDecision(decision="equivalent", target_memory_id=target_id),
    )
    assert receipt.outcome == "no_change"
    assert receipt.result["code"] == "target_not_in_evaluated_snapshot"


class _BumpMemoryDuringRelated(MemoryReconciliationService):
    async def _related_memories(self, session, candidate):
        async with self.factory() as other:
            row = await other.get(
                SharedResourceVersionRow,
                {"resource_type": "memory_state", "resource_id": "owner"},
            )
            if row is None:
                other.add(SharedResourceVersionRow(resource_type="memory_state", resource_id="owner", version=1))
            else:
                row.version += 1
            await other.commit()
        return await super()._related_memories(session, candidate)


@pytest.mark.asyncio
async def test_revision_change_during_evaluation_input_load_is_detected_at_publish(pg_factory):
    _, _, _, _ = await _seed_candidate(pg_factory)
    leases = MemoryCandidateLeaseRepository(pg_factory, lease_seconds=30, max_attempts=3)
    claim = (await leases.claim_batch(limit=1))[0][0]
    service = _BumpMemoryDuringRelated(
        pg_factory,
        _StaticModel({"decision": "create", "target_memory_id": None, "content": None, "reason": "test"}),
    )
    snapshot = await service._load_snapshot(claim)
    assert snapshot.memory_revision == 0
    receipt = await service.publisher.publish(claim, snapshot, ReconciliationDecision(decision="create"))
    assert receipt.outcome == "version_conflict"
    assert receipt.observed_version == 1


class _MutatingContinuityModel:
    def __init__(self, factory, transcript_id: UUID) -> None:
        self.factory = factory
        self.transcript_id = transcript_id

    async def complete_text(self, *, instructions: str, messages: list[dict[str, str]]) -> str:
        async with self.factory() as session:
            await session.execute(
                update(TranscriptRow)
                .where(TranscriptRow.id == self.transcript_id)
                .values(content_revision=TranscriptRow.content_revision + 1)
            )
            await session.commit()
        return "Stale summary that must not publish."


@pytest.mark.asyncio
async def test_continuity_cannot_publish_after_source_revision_changes(pg_factory):
    async with pg_factory() as session:
        transcript = TranscriptRow(
            kind="owner",
            next_turn_sequence=1,
            content_revision=1,
            closed_at=datetime.now(UTC),
            title="Closed",
        )
        session.add(transcript)
        await session.flush()
        session.add(TurnRow(
            transcript_id=transcript.id,
            sequence=1,
            actor="owner",
            blocks=[{"type": "text", "text": "Source before redaction."}],
        ))
        await session.commit()
        transcript_id = transcript.id

    result = await ContinuityCapsuleService(
        pg_factory, _MutatingContinuityModel(pg_factory, transcript_id)
    ).run_once()
    assert result.capsules_created == 0
    async with pg_factory() as session:
        assert (await session.execute(select(func.count()).select_from(ContinuityCapsuleRow))).scalar_one() == 0


class _DeletingChunkEmbeddingClient:
    model = "test-fence"
    dimensions = 1536

    def __init__(self, factory, chunk_id: UUID) -> None:
        self.factory = factory
        self.chunk_id = chunk_id

    async def embed(self, texts):
        async with self.factory() as session:
            row = await session.get(TranscriptIndexChunkRow, self.chunk_id)
            await session.delete(row)
            await session.commit()
        return [[0.0] * self.dimensions for _ in texts]


@pytest.mark.asyncio
async def test_stale_transcript_embedding_cannot_resurrect_deleted_chunk(pg_factory):
    async with pg_factory() as session:
        transcript = TranscriptRow(kind="owner", next_turn_sequence=1, content_revision=1)
        session.add(transcript)
        await session.flush()
        chunk = TranscriptIndexChunkRow(
            transcript_id=transcript.id,
            index_version="text-v1",
            start_sequence=1,
            end_sequence=1,
            source_turn_ids=[],
            source_revision=1,
            content="Delete me while embedding.",
        )
        session.add(chunk)
        await session.commit()
        chunk_id = chunk.id
    result = await TranscriptEmbeddingIndexer(
        pg_factory, _DeletingChunkEmbeddingClient(pg_factory, chunk_id)
    ).run_once(batch_size=1, max_chunks=1)
    assert result["chunks_embedded"] == 0
    async with pg_factory() as session:
        assert await session.get(TranscriptIndexChunkRow, chunk_id) is None


class _RetiringMemoryEmbeddingClient:
    model = "test-fence"
    dimensions = 1536

    def __init__(self, factory, memory_id: UUID) -> None:
        self.factory = factory
        self.memory_id = memory_id

    async def embed(self, texts):
        async with self.factory() as session:
            row = await session.get(DurableMemoryRow, self.memory_id)
            row.status = RETIRED
            row.suppresses_recall = True
            row.retired_at = datetime.now(UTC)
            await session.commit()
        return [[0.0] * self.dimensions for _ in texts]


@pytest.mark.asyncio
async def test_stale_durable_embedding_cannot_publish_after_memory_retired(pg_factory):
    async with pg_factory() as session:
        memory, _ = await DurableMemoryRepository(session).create_active(
            "Active only while embedding starts.",
            source_transcript_id=None,
            source_turn_id=None,
            record_kind="derived",
            memory_kind="fact",
            scope="cross_chat",
            scope_key="owner",
            durability="long_term",
        )
        await session.commit()
        memory_id = memory.id
    result = await DurableMemoryEmbeddingIndexer(
        pg_factory, _RetiringMemoryEmbeddingClient(pg_factory, memory_id)
    ).run_once(batch_size=1, max_memories=1)
    assert result["durable_memories_embedded"] == 0
    async with pg_factory() as session:
        row = await session.get(DurableMemoryRow, memory_id)
        assert row.status == RETIRED
        assert row.embedding is None


@pytest.mark.asyncio
async def test_expired_final_lease_is_failed_instead_of_sticking_forever(pg_factory):
    candidate_id, _, _, _ = await _seed_candidate(pg_factory)
    leases = MemoryCandidateLeaseRepository(pg_factory, lease_seconds=30, max_attempts=1)
    claim = (await leases.claim_batch(limit=1))[0][0]
    async with pg_factory() as session:
        candidate = await session.get(MemoryCandidateRow, candidate_id)
        candidate.leased_until = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()

    claims, _ = await leases.claim_batch(limit=1)
    assert claims == []
    async with pg_factory() as session:
        candidate = await session.get(MemoryCandidateRow, candidate_id)
        attempt = await session.get(MemoryReconciliationAttemptRow, claim.attempt_id)
        assert candidate.status == "failed"
        assert candidate.lease_token is None
        assert candidate.leased_until is None
        assert attempt.status == "attempt_limit"
        assert attempt.completed_at is not None


@pytest.mark.asyncio
async def test_owner_delete_during_inference_wins_and_invalidates_candidate(pg_factory):
    content = "Jaco prefers the old temporary setting."
    candidate_id, transcript_id, turn_id, _ = await _seed_candidate(pg_factory, content=content)
    async with pg_factory() as session:
        memory, _ = await DurableMemoryRepository(session).create_active(
            content,
            source_transcript_id=transcript_id,
            source_turn_id=turn_id,
            record_kind="owner_directed",
            memory_kind="preference",
            scope="cross_chat",
            scope_key="owner",
            durability="long_term",
            subject="Jaco",
        )
        await session.commit()
        memory_id = memory.id

    claim, snapshot, _ = await _claim_and_snapshot(pg_factory, candidate_id)
    deleted = await MemoryLifecycleCommands(pg_factory).delete(
        {"memory_id": str(memory_id), "scope": "memory_and_sources"}
    )
    assert deleted["result"] == "deleted"

    stale = await DerivedMemoryPublisher(pg_factory).publish(
        claim, snapshot, ReconciliationDecision(decision="create")
    )
    assert stale.outcome == "version_conflict"
    async with pg_factory() as session:
        candidate = await session.get(MemoryCandidateRow, candidate_id)
        memory = await session.get(DurableMemoryRow, memory_id)
        turn = await session.get(TurnRow, turn_id)
        assert candidate.status == "invalidated"
        assert candidate.lease_token is None
        assert candidate.content is None
        assert memory.status == "deleted"
        assert memory.content is None
        assert "Content deleted by owner" in str(turn.blocks)


@pytest.mark.asyncio
async def test_unresolved_project_scope_cannot_publish_durable_memory(pg_factory):
    candidate_id, _, _, _ = await _seed_candidate(
        pg_factory,
        content="This project currently uses a temporary parser.",
        kind="project_state",
        scope="project",
    )
    claim, snapshot, _ = await _claim_and_snapshot(pg_factory, candidate_id)
    assert snapshot.publication_allowed is False
    receipt = await DerivedMemoryPublisher(pg_factory).publish(
        claim, snapshot, ReconciliationDecision(decision="create")
    )
    assert receipt.outcome == "no_change"
    assert receipt.result["code"] == "scope_not_publishable"
    async with pg_factory() as session:
        assert (await session.execute(select(func.count()).select_from(DurableMemoryRow))).scalar_one() == 0


@pytest.mark.asyncio
async def test_chat_scope_survives_derived_publication(pg_factory):
    candidate_id, transcript_id, _, _ = await _seed_candidate(
        pg_factory,
        content="Use compact tables in this chat.",
        kind="preference",
        scope="chat",
    )
    claim, snapshot, _ = await _claim_and_snapshot(pg_factory, candidate_id)
    receipt = await DerivedMemoryPublisher(pg_factory).publish(
        claim, snapshot, ReconciliationDecision(decision="create")
    )
    assert receipt.outcome == "applied"
    async with pg_factory() as session:
        memory = (
            await session.execute(
                select(DurableMemoryRow).where(DurableMemoryRow.status == "active")
            )
        ).scalar_one()
        assert memory.record_kind == "derived"
        assert memory.scope == "chat"
        assert memory.scope_key == f"chat:{transcript_id}"


@pytest.mark.asyncio
async def test_end_to_end_candidate_reconciliation_creates_derived_memory(pg_factory):
    await _seed_candidate(
        pg_factory,
        content="Jaco prefers fresh topic-specific chats.",
        kind="preference",
        subject="Jaco",
    )
    model = _PipelineModel(
        content="Jaco prefers fresh topic-specific chats.",
        kind="preference",
        scope="cross_chat",
        durability="long_term",
        relation="new",
    )
    result = await MemoryReconciliationService(
        pg_factory,
        model,
        batch_size=4,
        lease_seconds=30,
        max_attempts=3,
    ).run_once()
    assert result.claimed == 1
    assert result.reconciled == 1
    assert result.failures == 0
    async with pg_factory() as session:
        memory = (
            await session.execute(
                select(DurableMemoryRow).where(DurableMemoryRow.status == "active")
            )
        ).scalar_one()
        candidate = (
            await session.execute(select(MemoryCandidateRow))
        ).scalar_one()
        attempt = (
            await session.execute(select(MemoryReconciliationAttemptRow))
        ).scalar_one()
        assert memory.record_kind == "derived"
        assert memory.content == "Jaco prefers fresh topic-specific chats."
        assert candidate.status == "reconciled"
        assert attempt.status == "committed"
        assert attempt.evaluated_memory_revision == 0
        assert attempt.evaluated_source_revision == 1
