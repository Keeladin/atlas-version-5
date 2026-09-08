from uuid import UUID, uuid4

import pytest
from atlas.memory.durable import FAILED, FORGOTTEN, SUPERSEDED, MemoryMutationError
from atlas.memory.indexer import TranscriptIndexer
from atlas.memory.service import MemoryService
from atlas.persistence.models import (
    ContinuityCapsuleRow,
    DurableMemoryRow,
    MemoryCommandRow,
    TranscriptRow,
    TurnRow,
)
from sqlalchemy import func, select


class _MemoryEmbeddingClient:
    model = "test-memory-embedding-v1"
    dimensions = 1_536

    async def embed(self, texts):
        vectors = []
        for text in texts:
            lowered = text.casefold()
            related = any(
                term in lowered
                for term in ("roses", "phrase", "starling", "bird", "feathered")
            )
            vectors.append(([1.0, 0.0] if related else [0.0, 1.0]) + [0.0] * 1_534)
        return vectors


async def _seed_transcript(
    pg_factory, owner_text: str, atlas_text: str | None = None
) -> tuple[UUID, UUID]:
    async with pg_factory() as session:
        transcript = TranscriptRow(kind="owner", next_turn_sequence=2 if atlas_text else 1)
        session.add(transcript)
        await session.flush()
        owner_turn = TurnRow(
            id=uuid4(), transcript_id=transcript.id, sequence=1, actor="owner",
            blocks=[{"type": "text", "text": owner_text}],
        )
        session.add(owner_turn)
        if atlas_text is not None:
            session.add(TurnRow(
                id=uuid4(), transcript_id=transcript.id, sequence=2, actor="atlas",
                blocks=[{"type": "text", "text": atlas_text}],
            ))
        await session.commit()
        return transcript.id, owner_turn.id


async def _index_all(pg_factory) -> None:
    async with pg_factory() as session:
        await TranscriptIndexer(session, max_chars=4_000).run_once(active_tail_exchanges=0)
        await session.commit()


@pytest.mark.asyncio
async def test_remember_is_durable_idempotent_and_owner_sourced(pg_factory) -> None:
    _, owner_turn_id = await _seed_transcript(
        pg_factory, "Remember that my workshop bird is a starling."
    )
    service = MemoryService(pg_factory)

    first = await service.remember({"content": "My workshop bird is a starling."})
    second = await service.remember({"content": "  my workshop bird is a STARLING.  "})

    assert first["status"] == "applied"
    assert first["result"] == "created"
    assert second["status"] == "applied"
    assert second["result"] == "already_active"
    assert first["memory"]["memory_id"] == second["memory"]["memory_id"]
    assert first["memory"]["source_turn_id"] == str(owner_turn_id)

    async with pg_factory() as session:
        memories = (await session.execute(select(func.count()).select_from(DurableMemoryRow))).scalar_one()
        commands = (await session.execute(select(func.count()).select_from(MemoryCommandRow))).scalar_one()
        assert memories == 1
        assert commands == 2


@pytest.mark.asyncio
async def test_forget_legacy_phrase_preserves_transcript_but_blocks_recall(pg_factory) -> None:
    phrase = "Roses are red, violets are blue."
    transcript_id, owner_turn_id = await _seed_transcript(
        pg_factory,
        f"Hey remember this phrase: {phrase}",
        f"Got it: {phrase}",
    )
    await _index_all(pg_factory)
    service = MemoryService(pg_factory, embedder=_MemoryEmbeddingClient())

    before = await service.search({"query": "phrase roses violets", "limit": 5})
    assert any("Roses are red" in str(item["content"]) for item in before["results"])

    forgotten = await service.forget({"content": phrase})
    assert forgotten["status"] == "applied"
    assert forgotten["result"] == "tombstone_created"

    after = await service.search({"query": "phrase roses violets", "limit": 5})
    assert after["durable_memories"] == []
    assert all("Roses are red" not in str(item["content"]) for item in after["results"])
    assert after["memory_policy"]["suppression_guards"] == 1

    async with pg_factory() as session:
        original = await session.get(TurnRow, owner_turn_id)
        assert original is not None
        assert phrase in str(original.blocks)
        transcript = await session.get(TranscriptRow, transcript_id)
        assert transcript is not None
        guard = (
            await session.execute(
                select(DurableMemoryRow).where(DurableMemoryRow.status == FORGOTTEN)
            )
        ).scalar_one()
        assert guard.content == phrase
        assert guard.suppresses_recall is True


@pytest.mark.asyncio
async def test_re_remember_after_forget_clears_suppression(pg_factory) -> None:
    await _seed_transcript(pg_factory, "Remember that the signal phrase is roses are red.")
    service = MemoryService(pg_factory)
    remembered = await service.remember({"content": "The signal phrase is roses are red."})
    memory_id = remembered["memory"]["memory_id"]

    forgotten = await service.forget({"memory_id": memory_id})
    assert forgotten["result"] == "forgotten"
    restored = await service.remember({"content": "The signal phrase is roses are red."})
    assert restored["result"] == "created"

    searched = await service.search({"query": "signal phrase roses", "limit": 5})
    assert len(searched["durable_memories"]) == 1
    assert searched["durable_memories"][0]["content"] == "The signal phrase is roses are red."
    assert searched["memory_policy"]["suppression_guards"] == 0

    async with pg_factory() as session:
        rows = list((await session.execute(select(DurableMemoryRow))).scalars())
        assert sum(row.status == "active" for row in rows) == 1
        old = next(row for row in rows if row.status == FORGOTTEN)
        assert old.suppresses_recall is False


@pytest.mark.asyncio
async def test_legacy_correction_supersedes_old_transcript_recall(pg_factory) -> None:
    old = "My preferred bird is a starling."
    new = "My preferred bird is a raven."
    await _seed_transcript(pg_factory, old, "I will remember that preference.")
    await _index_all(pg_factory)
    service = MemoryService(pg_factory, embedder=_MemoryEmbeddingClient())

    corrected = await service.correct({"old_content": old, "content": new})
    assert corrected["status"] == "applied"
    assert corrected["memory"]["content"] == new

    searched = await service.search({"query": "preferred bird", "limit": 5})
    assert searched["durable_memories"][0]["content"] == new

    assert all(old not in str(item["content"]) for item in searched["results"])
    assert searched["memory_policy"]["suppression_guards"] == 1

    async with pg_factory() as session:
        rows = list((await session.execute(select(DurableMemoryRow))).scalars())
        guard = next(row for row in rows if row.content == old)
        replacement = next(row for row in rows if row.content == new)
        assert guard.status == SUPERSEDED
        assert guard.suppresses_recall is True
        assert replacement.status == "active"
        assert replacement.supersedes_id == guard.id


@pytest.mark.asyncio
async def test_failed_memory_command_is_auditable(pg_factory) -> None:
    await _seed_transcript(pg_factory, "Forget the impossible memory target.")
    service = MemoryService(pg_factory)

    with pytest.raises(MemoryMutationError):
        await service.forget({"memory_id": str(uuid4())})

    ledger = await service.commands({})
    assert ledger["state"]["failed_commands"] == 1
    assert ledger["state"]["pending_commands"] == 0
    assert ledger["commands"][0]["operation"] == "forget"
    assert ledger["commands"][0]["status"] == FAILED
    assert "target memory does not exist" in str(ledger["commands"][0]["error"])


@pytest.mark.asyncio
async def test_durable_memory_embeddings_join_background_maintenance(pg_factory) -> None:
    await _seed_transcript(pg_factory, "Remember my preferred bird.")
    service = MemoryService(pg_factory, embedder=_MemoryEmbeddingClient())
    remembered = await service.remember({"content": "My preferred bird is a starling."})

    maintenance = await service.index_once(active_tail_exchanges=10)
    assert maintenance["embedding_status"] == "ready"
    assert maintenance["durable_memories_embedded"] == 1
    assert maintenance["durable_embedding_batches"] == 1

    searched = await service.search({"query": "feathered companion", "limit": 5})
    assert searched["durable_memories"]
    item = searched["durable_memories"][0]
    assert item["memory_id"] == remembered["memory"]["memory_id"]
    assert item["lexical_rank"] is None
    assert "semantic" in item["retrieval_sources"]
    assert float(item["semantic_similarity"]) > 0.99

    second = await service.index_once(active_tail_exchanges=10)
    assert second["durable_memories_embedded"] == 0


@pytest.mark.asyncio
async def test_forgetting_clears_vector_and_does_not_reembed_guard(pg_factory) -> None:
    await _seed_transcript(pg_factory, "Remember my temporary bird preference.")
    service = MemoryService(pg_factory, embedder=_MemoryEmbeddingClient())
    remembered = await service.remember({"content": "My temporary bird is a starling."})
    memory_id = UUID(remembered["memory"]["memory_id"])

    first = await service.index_once(active_tail_exchanges=10)
    assert first["durable_memories_embedded"] == 1
    async with pg_factory() as session:
        row = await session.get(DurableMemoryRow, memory_id)
        assert row is not None and row.embedding is not None

    await service.forget({"memory_id": str(memory_id)})
    async with pg_factory() as session:
        row = await session.get(DurableMemoryRow, memory_id)
        assert row is not None
        assert row.status == FORGOTTEN
        assert row.embedding is None
        assert row.embedding_model is None

    second = await service.index_once(active_tail_exchanges=10)
    assert second["durable_memories_embedded"] == 0


@pytest.mark.asyncio
async def test_forget_with_stale_memory_id_does_not_fallback_to_content(pg_factory) -> None:
    await _seed_transcript(pg_factory, "Forget a stale memory reference.")
    service = MemoryService(pg_factory)

    with pytest.raises(MemoryMutationError):
        await service.forget({
            "memory_id": str(uuid4()),
            "content": "This must not become a fallback tombstone.",
        })

    ledger = await service.commands({})
    assert ledger["state"]["failed_commands"] == 1
    async with pg_factory() as session:
        memories = (
            await session.execute(select(func.count()).select_from(DurableMemoryRow))
        ).scalar_one()
        assert memories == 0


@pytest.mark.asyncio
async def test_repeated_legacy_forget_reuses_existing_tombstone(pg_factory) -> None:
    phrase = "Roses are red, violets are blue."
    await _seed_transcript(pg_factory, f"Remember this phrase: {phrase}")
    service = MemoryService(pg_factory)

    first = await service.forget({"content": phrase})
    second = await service.forget({"content": phrase})

    assert first["result"] == "tombstone_created"
    assert second["result"] == "already_forgotten"
    assert first["memory_id"] == second["memory_id"]

    async with pg_factory() as session:
        rows = list((await session.execute(select(DurableMemoryRow))).scalars())
        assert len(rows) == 1
        assert rows[0].status == FORGOTTEN
        assert rows[0].suppresses_recall is True

@pytest.mark.asyncio
async def test_forget_invalidates_derived_continuity_capsules(pg_factory) -> None:
    transcript_id, _ = await _seed_transcript(pg_factory, "Remember the temporary continuity detail.")
    async with pg_factory() as session:
        session.add(ContinuityCapsuleRow(
            transcript_id=transcript_id, revision=1, start_sequence=1, end_sequence=1,
            summary="A paraphrased continuity detail that must be rebuilt after forgetting.",
        ))
        await session.commit()

    service = MemoryService(pg_factory)
    await service.forget({"content": "the temporary continuity detail"})

    async with pg_factory() as session:
        assert (await session.execute(
            select(func.count()).select_from(ContinuityCapsuleRow)
        )).scalar_one() == 0

@pytest.mark.asyncio
async def test_re_remember_invalidates_capsules_after_clearing_forget_guard(pg_factory) -> None:
    transcript_id, _ = await _seed_transcript(pg_factory, "Remember the temporary continuity detail.")
    service = MemoryService(pg_factory)
    remembered = await service.remember({"content": "The temporary continuity detail."})
    await service.forget({"memory_id": remembered["memory"]["memory_id"]})

    async with pg_factory() as session:
        session.add(ContinuityCapsuleRow(
            transcript_id=transcript_id, revision=1, start_sequence=1, end_sequence=1,
            summary="The suppressed detail is absent from this derived handoff.",
        ))
        await session.commit()

    await service.remember({"content": "The temporary continuity detail."})

    async with pg_factory() as session:
        assert (await session.execute(
            select(func.count()).select_from(ContinuityCapsuleRow)
        )).scalar_one() == 0
