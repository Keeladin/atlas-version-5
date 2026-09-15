import pytest
from atlas.memory.durable import clean_memory_content, memory_fingerprint
from atlas.memory.indexer import TranscriptIndexer
from atlas.persistence.models import (
    DurableMemoryRow,
    MemoryCandidateRow,
    MemoryProvenanceRow,
    TranscriptIndexStateRow,
    TranscriptRow,
    TurnRow,
)
from atlas.transcript.models import Actor, TextBlock
from atlas.transcript.repository import TranscriptRepository
from sqlalchemy import select


@pytest.mark.asyncio
async def test_owner_chats_have_separate_transcripts_and_can_be_switched(pg_factory):
    async with pg_factory() as session:
        repo = TranscriptRepository(session)
        atlas = await repo.get_or_create_active()
        await repo.rename_owner_chat(atlas.id, "Atlas")
        await repo.append_turn(atlas.id, Actor.OWNER, [TextBlock(text="Atlas work")])
        bus = await repo.create_owner_chat(title="Bus tickets")
        await repo.append_turn(bus.id, Actor.OWNER, [TextBlock(text="Find a bus")])
        await session.commit()

    async with pg_factory() as session:
        repo = TranscriptRepository(session)
        active = await repo.get_or_create_active()
        assert active.id == bus.id
        assert [turn.blocks[0].text for turn in await repo.list_turns(bus.id)] == ["Find a bus"]
        assert [turn.blocks[0].text for turn in await repo.list_turns(atlas.id)] == ["Atlas work"]

        selected = await repo.activate_owner_chat(atlas.id)
        await session.commit()
        assert selected.id == atlas.id
        rows = list((await session.execute(
            select(TranscriptRow).where(TranscriptRow.kind == "owner").order_by(TranscriptRow.title)
        )).scalars())
        assert sum(row.closed_at is None for row in rows) == 1
        assert next(row for row in rows if row.id == atlas.id).closed_at is None
        assert next(row for row in rows if row.id == bus.id).closed_at is not None


@pytest.mark.asyncio
async def test_first_owner_turn_suggests_title_for_new_chat(pg_factory):
    async with pg_factory() as session:
        repo = TranscriptRepository(session)
        await repo.get_or_create_active()
        chat = await repo.create_owner_chat()
        await repo.append_turn(chat.id, Actor.OWNER, [TextBlock(text="Bus tickets from Pretoria to Cape Town")])
        await session.commit()

    async with pg_factory() as session:
        chat = await TranscriptRepository(session).get_owner_chat(chat.id)
        assert chat.title == "Bus tickets from Pretoria to Cape Town"


@pytest.mark.asyncio
async def test_delete_chat_tombstones_history_and_preserves_verified_memory_graph(pg_factory):
    async with pg_factory() as session:
        repo = TranscriptRepository(session)
        chat = await repo.get_or_create_active()
        turn = await repo.append_turn(chat.id, Actor.OWNER, [TextBlock(text="Remember my preference")])
        content = clean_memory_content("Prefers morning flights")
        memory = DurableMemoryRow(
            content=content,
            fingerprint=memory_fingerprint(content),
            grounding_status="verified",
            owner_assertion_turn_id=turn.id,
            source_transcript_id=chat.id,
            source_turn_id=turn.id,
        )
        candidate = MemoryCandidateRow(
            status="blocked",
            kind="preference",
            content="Blocked candidate payload",
            scope="cross_chat",
            confidence=0.9,
            durability="long_term",
            source_transcript_id=chat.id,
            source_turn_id=turn.id,
        )
        session.add_all([memory, candidate])
        await session.flush()
        provenance = MemoryProvenanceRow(
            memory_id=memory.id, relationship="candidate_source", source_candidate_id=candidate.id,
        )
        session.add(provenance)
        session.add(TranscriptIndexStateRow(
            transcript_id=chat.id, index_version="text-v1", last_indexed_sequence=1,
        ))
        await session.commit()
        memory_id, candidate_id, provenance_id = memory.id, candidate.id, provenance.id

        replacement = await repo.delete_owner_chat(chat.id)
        await session.commit()
        assert replacement.id != chat.id

    async with pg_factory() as session:
        repo = TranscriptRepository(session)
        with pytest.raises(LookupError):
            await repo.get_owner_chat(chat.id)
        assert chat.id not in {item.id for item in await repo.list_owner_chats()}

        tombstone = await session.get(TranscriptRow, chat.id)
        assert tombstone is not None
        assert tombstone.kind == "owner_deleted"
        assert tombstone.title is None
        assert tombstone.context_summary is None

        turns = list((await session.execute(
            select(TurnRow).where(TurnRow.transcript_id == chat.id)
        )).scalars())
        assert len(turns) == 1
        assert turns[0].deleted_at is not None
        assert turns[0].blocks == [{"type": "text", "text": "[Chat deleted by owner]"}]
        assert await session.get(TranscriptIndexStateRow, {
            "transcript_id": chat.id, "index_version": "text-v1",
        }) is None

        memory = await session.get(DurableMemoryRow, memory_id)
        assert memory is not None
        assert memory.grounding_status == "verified"
        assert memory.owner_assertion_turn_id == turn.id
        assert memory.source_transcript_id == chat.id
        assert memory.source_turn_id == turn.id
        candidate = await session.get(MemoryCandidateRow, candidate_id)
        assert candidate is not None and candidate.status == "invalidated"
        assert candidate.content is None
        assert candidate.fingerprint is None
        assert candidate.evidence is None
        assert candidate.decision_json == {}
        assert candidate.invalidated_at is not None
        provenance = await session.get(MemoryProvenanceRow, provenance_id)
        assert provenance is not None and provenance.source_candidate_id == candidate_id

@pytest.mark.asyncio
async def test_closed_chat_becomes_fully_searchable_on_next_maintenance_pass(pg_factory):
    async with pg_factory() as session:
        repo = TranscriptRepository(session)
        previous = await repo.get_or_create_active()
        await repo.append_turn(previous.id, Actor.OWNER, [TextBlock(text="We were building the Normalizer project")])
        await repo.append_turn(previous.id, Actor.ATLAS, [TextBlock(text="Normalizer is a separate browser application")])
        current = await repo.create_owner_chat(title="Fresh topic")
        await repo.append_turn(current.id, Actor.OWNER, [TextBlock(text="Start something else")])
        await session.commit()

    async with pg_factory() as session:
        result = await TranscriptIndexer(session).run_once(active_tail_exchanges=10)
        await session.commit()
        assert result.turns_processed == 2
        previous_state = await session.get(
            TranscriptIndexStateRow,
            {"transcript_id": previous.id, "index_version": "text-v1"},
        )
        current_state = await session.get(
            TranscriptIndexStateRow,
            {"transcript_id": current.id, "index_version": "text-v1"},
        )
        assert previous_state is not None
        assert previous_state.last_indexed_sequence == 2
        assert current_state is None
