from uuid import uuid4

import pytest
from atlas.memory.candidates import MemoryCandidateIntake
from atlas.persistence.models import MemoryCandidateRow, TranscriptRow, TurnRow
from sqlalchemy import func, select


@pytest.mark.asyncio
async def test_candidate_intake_binds_runtime_provenance(pg_factory) -> None:
    async with pg_factory() as session:
        transcript = TranscriptRow(kind="owner", next_turn_sequence=2)
        session.add(transcript)
        await session.flush()
        owner = TurnRow(
            id=uuid4(), transcript_id=transcript.id, sequence=1, actor="owner",
            blocks=[{"type": "text", "text": "I prefer fresh topic chats."}],
        )
        provider = TurnRow(
            id=uuid4(), transcript_id=transcript.id, sequence=2, actor="tool",
            blocks=[],
        )
        session.add_all([owner, provider])
        await session.commit()
        transcript_id, owner_id, provider_id = transcript.id, owner.id, provider.id

    result = await MemoryCandidateIntake(pg_factory).enqueue_many(
        [{
            "kind": "preference",
            "content": "Jaco prefers fresh topic-specific chats.",
            "scope": "cross_chat",
            "confidence": 0.93,
            "durability": "long_term",
            "proposed_action": "upsert",
            "subject": "Jaco",
            "namespace": "interaction_preferences",
        }],
        source_transcript_id=transcript_id,
        source_turn_id=owner_id,
        source_provider_evidence_id=provider_id,
    )

    assert result == {"accepted": 1, "duplicate": 0, "rejected": 0}
    async with pg_factory() as session:
        row = (await session.execute(select(MemoryCandidateRow))).scalar_one()
        assert row.status == "pending"
        assert row.source_turn_id == owner_id
        assert row.source_provider_evidence_id == provider_id
        assert row.proposed_action == "upsert"


@pytest.mark.asyncio
async def test_candidate_intake_rejects_invalid_and_deduplicates_pending(pg_factory) -> None:
    async with pg_factory() as session:
        transcript = TranscriptRow(kind="owner", next_turn_sequence=1)
        session.add(transcript)
        await session.flush()
        owner = TurnRow(
            id=uuid4(), transcript_id=transcript.id, sequence=1, actor="owner",
            blocks=[{"type": "text", "text": "Stable preference."}],
        )
        session.add(owner)
        await session.commit()
        transcript_id, owner_id = transcript.id, owner.id

    candidate = {
        "kind": "preference", "content": "Stable preference.",
        "scope": "cross_chat", "confidence": 0.9,
        "durability": "long_term", "proposed_action": "upsert",
    }
    intake = MemoryCandidateIntake(pg_factory)
    first = await intake.enqueue_many(
        [
            candidate,
            {**candidate, "confidence": 2.0},
            {**candidate, "source_turn": "model-must-not-supply-this"},
        ],
        source_transcript_id=transcript_id, source_turn_id=owner_id,
        source_provider_evidence_id=None,
    )
    second = await intake.enqueue_many(
        [candidate],
        source_transcript_id=transcript_id, source_turn_id=owner_id,
        source_provider_evidence_id=None,
    )

    assert first == {"accepted": 1, "duplicate": 0, "rejected": 2}
    assert second == {"accepted": 0, "duplicate": 1, "rejected": 0}
    async with pg_factory() as session:
        count = (
            await session.execute(select(func.count()).select_from(MemoryCandidateRow))
        ).scalar_one()
        assert count == 1


@pytest.mark.asyncio
async def test_pending_candidate_has_no_memory_search_authority(pg_factory) -> None:
    from atlas.memory.service import MemoryService

    async with pg_factory() as session:
        transcript = TranscriptRow(kind="owner", next_turn_sequence=1)
        session.add(transcript)
        await session.flush()
        owner = TurnRow(
            id=uuid4(), transcript_id=transcript.id, sequence=1, actor="owner",
            blocks=[{"type": "text", "text": "Ordinary unrelated source wording."}],
        )
        session.add(owner)
        await session.commit()
        transcript_id, owner_id = transcript.id, owner.id

    await MemoryCandidateIntake(pg_factory).enqueue_many(
        [{"kind": "preference", "content": "Prefers comet-shaped widgets.",
          "scope": "cross_chat", "confidence": 0.95,
          "durability": "long_term", "proposed_action": "upsert"}],
        source_transcript_id=transcript_id, source_turn_id=owner_id,
        source_provider_evidence_id=None,
    )
    result = await MemoryService(pg_factory).search(
        {"query": "comet-shaped widgets", "limit": 5}
    )
    assert result["durable_memories"] == []
    assert result["results"] == []


@pytest.mark.asyncio
async def test_concurrent_duplicate_candidates_have_one_pending_winner(pg_factory) -> None:
    import asyncio

    async with pg_factory() as session:
        transcript = TranscriptRow(kind="owner", next_turn_sequence=1)
        session.add(transcript)
        await session.flush()
        owner = TurnRow(
            id=uuid4(), transcript_id=transcript.id, sequence=1, actor="owner",
            blocks=[{"type": "text", "text": "Same stable preference."}],
        )
        session.add(owner)
        await session.commit()
        transcript_id, owner_id = transcript.id, owner.id

    candidate = [{
        "kind": "preference", "content": "Same stable preference.",
        "scope": "cross_chat", "confidence": 0.9,
        "durability": "long_term", "proposed_action": "upsert",
    }]
    async def enqueue_once():
        return await MemoryCandidateIntake(pg_factory).enqueue_many(
            candidate, source_transcript_id=transcript_id,
            source_turn_id=owner_id, source_provider_evidence_id=None,
        )

    results = await asyncio.gather(enqueue_once(), enqueue_once())
    assert sum(item["accepted"] for item in results) == 1
    assert sum(item["duplicate"] for item in results) == 1
    async with pg_factory() as session:
        count = (
            await session.execute(select(func.count()).select_from(MemoryCandidateRow))
        ).scalar_one()
        assert count == 1


@pytest.mark.asyncio
async def test_chat_scoped_candidate_identity_is_bound_to_source_chat(pg_factory) -> None:
    intake = MemoryCandidateIntake(pg_factory)
    sources = []
    async with pg_factory() as session:
        for text in ("Chat one preference.", "Chat two preference."):
            transcript = TranscriptRow(kind="owner", next_turn_sequence=1, closed_at=None)
            session.add(transcript)
            await session.flush()
            owner = TurnRow(
                id=uuid4(), transcript_id=transcript.id, sequence=1, actor="owner",
                blocks=[{"type": "text", "text": text}],
            )
            session.add(owner)
            await session.flush()
            sources.append((transcript.id, owner.id))
            transcript.closed_at = func.now()
        await session.commit()

    candidate = [{
        "kind": "preference",
        "content": "Use compact tables in this chat.",
        "scope": "chat",
        "confidence": 0.9,
        "durability": "short_term",
        "proposed_action": "upsert",
    }]
    results = []
    for transcript_id, owner_id in sources:
        results.append(await intake.enqueue_many(
            candidate, source_transcript_id=transcript_id,
            source_turn_id=owner_id, source_provider_evidence_id=None,
        ))
    assert [item["accepted"] for item in results] == [1, 1]

    async with pg_factory() as session:
        rows = list((await session.execute(
            select(MemoryCandidateRow).order_by(MemoryCandidateRow.created_at)
        )).scalars())
        assert len(rows) == 2
        assert rows[0].scope_key != rows[1].scope_key
        assert rows[0].fingerprint != rows[1].fingerprint
        assert all(str(row.scope_key).startswith("chat:") for row in rows)


@pytest.mark.asyncio
async def test_cross_chat_candidate_identity_deduplicates_across_source_chats(pg_factory) -> None:
    intake = MemoryCandidateIntake(pg_factory)
    sources = []
    async with pg_factory() as session:
        for text in ("First statement.", "Second statement."):
            transcript = TranscriptRow(kind="owner", next_turn_sequence=1, closed_at=None)
            session.add(transcript)
            await session.flush()
            owner = TurnRow(
                id=uuid4(), transcript_id=transcript.id, sequence=1, actor="owner",
                blocks=[{"type": "text", "text": text}],
            )
            session.add(owner)
            await session.flush()
            sources.append((transcript.id, owner.id))
            transcript.closed_at = func.now()
        await session.commit()

    candidate = [{
        "kind": "preference",
        "content": "Jaco prefers fresh topic-specific chats.",
        "scope": "cross_chat",
        "confidence": 0.95,
        "durability": "long_term",
        "proposed_action": "upsert",
    }]
    first = await intake.enqueue_many(
        candidate, source_transcript_id=sources[0][0], source_turn_id=sources[0][1],
        source_provider_evidence_id=None,
    )
    second = await intake.enqueue_many(
        candidate, source_transcript_id=sources[1][0], source_turn_id=sources[1][1],
        source_provider_evidence_id=None,
    )
    assert first["accepted"] == 1
    assert second["duplicate"] == 1

    async with pg_factory() as session:
        row = (await session.execute(select(MemoryCandidateRow))).scalar_one()
        assert row.scope_key == "owner"
