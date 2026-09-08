import pytest
from atlas.memory.durable import clean_memory_content, memory_fingerprint
from atlas.persistence.models import (
    DurableMemoryRow,
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
async def test_delete_chat_removes_chat_history_but_keeps_durable_memory(pg_factory):
    async with pg_factory() as session:
        repo = TranscriptRepository(session)
        chat = await repo.get_or_create_active()
        turn = await repo.append_turn(chat.id, Actor.OWNER, [TextBlock(text="Remember my preference")])
        content = clean_memory_content("Prefers morning flights")
        memory = DurableMemoryRow(
            content=content,
            fingerprint=memory_fingerprint(content),
            source_transcript_id=chat.id,
            source_turn_id=turn.id,
        )
        session.add(memory)
        session.add(TranscriptIndexStateRow(transcript_id=chat.id, index_version="text-v1", last_indexed_sequence=1))
        await session.commit()
        memory_id = memory.id

        replacement = await repo.delete_owner_chat(chat.id)
        await session.commit()
        assert replacement.id != chat.id

    async with pg_factory() as session:
        assert await session.get(TranscriptRow, chat.id) is None
        assert (await session.execute(select(TurnRow).where(TurnRow.transcript_id == chat.id))).first() is None
        assert await session.get(TranscriptIndexStateRow, {"transcript_id": chat.id, "index_version": "text-v1"}) is None
        memory = await session.get(DurableMemoryRow, memory_id)
        assert memory is not None
        assert memory.source_transcript_id is None
        assert memory.source_turn_id is None
