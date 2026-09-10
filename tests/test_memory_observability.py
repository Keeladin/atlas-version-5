from datetime import UTC, datetime
from uuid import uuid4

import pytest
from atlas.memory.durable import DurableMemoryRepository
from atlas.memory.observability import MemoryObservabilityService
from atlas.persistence.models import (
    MemoryCandidateRow,
    MemoryProvenanceRow,
    MemoryReconciliationAttemptRow,
    TranscriptRow,
    TurnRow,
)
from sqlalchemy import select


@pytest.mark.asyncio
async def test_observability_projects_candidate_decision_and_resulting_memory(pg_factory) -> None:
    async with pg_factory() as session:
        transcript = TranscriptRow(kind="owner", title="Memory calibration", next_turn_sequence=1)
        session.add(transcript)
        await session.flush()
        source = TurnRow(
            transcript_id=transcript.id,
            sequence=1,
            actor="owner",
            blocks=[{"type": "text", "text": "I prefer quiet music while doing deep technical work."}],
        )
        session.add(source)
        await session.flush()
        candidate = MemoryCandidateRow(
            status="reconciled",
            kind="preference",
            content="Prefers quiet music during deep technical work.",
            scope="cross_chat",
            scope_key="owner",
            confidence=0.92,
            durability="long_term",
            proposed_action="upsert",
            subject="Jaco",
            namespace="music",
            evidence="Owner explicitly described a situational preference.",
            fingerprint="a" * 64,
            source_transcript_id=transcript.id,
            source_turn_id=source.id,
            decision_json={"decision": "create", "result": "created"},
            processed_at=datetime.now(UTC),
        )
        session.add(candidate)
        await session.flush()
        memory, created = await DurableMemoryRepository(session).create_active(
            "Prefers quiet music during deep technical work.",
            source_transcript_id=transcript.id,
            source_turn_id=source.id,
            record_kind="derived",
            memory_kind="preference",
            scope="cross_chat",
            scope_key="owner",
            durability="long_term",
            subject="Jaco",
            namespace="music",
        )
        assert created is True
        attempt = MemoryReconciliationAttemptRow(
            candidate_id=candidate.id,
            lease_token=uuid4(),
            attempt_number=1,
            evaluated_memory_revision=4,
            evaluated_source_revision=1,
            semantic_decision="create",
            target_memory_id=memory.id,
            status="committed",
            evidence_json={"active_memory_ids": []},
            result_json={"result": "created"},
            completed_at=datetime.now(UTC),
        )
        session.add_all([
            attempt,
            MemoryProvenanceRow(
                memory_id=memory.id,
                relationship="candidate_source",
                source_candidate_id=candidate.id,
                source_turn_id=source.id,
            ),
        ])
        await session.commit()
        candidate_id = candidate.id

    async with pg_factory() as session:
        service = MemoryObservabilityService(session)
        overview = await service.overview(limit=10)
        assert overview["summary"]["candidate_counts"]["reconciled"] == 1
        assert overview["summary"]["memory_counts"]["active"] == 1
        assert overview["summary"]["authority_counts"] == {"owner": 0, "derived": 1}
        assert overview["summary"]["last_attempt"]["semantic_decision"] == "create"
        listed = overview["recent_candidates"][0]
        assert listed["source_chat_title"] == "Memory calibration"
        assert listed["source_sequence"] == 1
        assert listed["latest_attempt"]["result"] == {"result": "created"}

        detail = await service.candidate_detail(candidate_id)
        assert detail["source"]["text"] == "I prefer quiet music while doing deep technical work."
        assert detail["candidate"]["decision"] == {"decision": "create", "result": "created"}
        assert detail["attempts"][0]["evaluated_memory_revision"] == 4
        assert detail["linked_memories"][0]["content"] == "Prefers quiet music during deep technical work."
        assert detail["linked_memories"][0]["provenance"][0]["relationship"] == "candidate_source"
        assert detail["reason_retained"] is False


@pytest.mark.asyncio
async def test_observability_never_returns_deleted_source_text(pg_factory) -> None:
    async with pg_factory() as session:
        transcript = TranscriptRow(kind="owner", title="Deleted source", next_turn_sequence=1)
        session.add(transcript)
        await session.flush()
        source = TurnRow(
            transcript_id=transcript.id,
            sequence=1,
            actor="owner",
            blocks=[{"type": "text", "text": "Sensitive source text that must not be projected."}],
            deleted_at=datetime.now(UTC),
        )
        session.add(source)
        await session.flush()
        candidate = MemoryCandidateRow(
            status="invalidated",
            kind="fact",
            content=None,
            scope="cross_chat",
            scope_key="owner",
            confidence=0.9,
            durability="long_term",
            proposed_action="upsert",
            subject=None,
            namespace=None,
            evidence=None,
            fingerprint=None,
            source_transcript_id=transcript.id,
            source_turn_id=source.id,
            invalidated_at=datetime.now(UTC),
            processed_at=datetime.now(UTC),
        )
        session.add(candidate)
        await session.commit()
        candidate_id = candidate.id

    async with pg_factory() as session:
        detail = await MemoryObservabilityService(session).candidate_detail(candidate_id)
        assert detail["source"]["deleted"] is True
        assert detail["source"]["text"] is None
        assert detail["candidate"]["content"] is None
        assert detail["candidate"]["evidence"] is None

@pytest.mark.asyncio
async def test_observability_change_token_tracks_pipeline_state_transitions(pg_factory) -> None:
    async with pg_factory() as session:
        service = MemoryObservabilityService(session)
        empty_token = await service.change_token()

        transcript = TranscriptRow(kind="owner", title="Live memory", next_turn_sequence=1)
        session.add(transcript)
        await session.flush()
        source = TurnRow(
            transcript_id=transcript.id, sequence=1, actor="owner",
            blocks=[{"type": "text", "text": "Rememberable fact."}],
        )
        session.add(source)
        await session.flush()
        candidate = MemoryCandidateRow(
            status="pending", kind="fact", content="Rememberable fact.",
            scope="cross_chat", scope_key="owner", confidence=0.9,
            durability="long_term", proposed_action="upsert", fingerprint="b" * 64,
            source_transcript_id=transcript.id, source_turn_id=source.id,
        )
        session.add(candidate)
        await session.commit()

    async with pg_factory() as session:
        pending_token = await MemoryObservabilityService(session).change_token()
        assert pending_token != empty_token
        candidate = (await session.execute(select(MemoryCandidateRow))).scalar_one()
        candidate.status = "leased"
        candidate.leased_until = datetime.now(UTC)
        session.add(MemoryReconciliationAttemptRow(
            candidate_id=candidate.id, lease_token=uuid4(), attempt_number=1, status="claimed",
        ))
        await session.commit()

    async with pg_factory() as session:
        leased_token = await MemoryObservabilityService(session).change_token()
        assert leased_token != pending_token
        attempt = (await session.execute(select(MemoryReconciliationAttemptRow))).scalar_one()
        attempt.status = "evaluated"
        attempt.semantic_decision = "create"
        await session.commit()

    async with pg_factory() as session:
        evaluated_token = await MemoryObservabilityService(session).change_token()
        assert evaluated_token != leased_token

