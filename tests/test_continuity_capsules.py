import pytest
from atlas.memory.continuity import ContinuityCapsuleService, recent_continuity_context
from atlas.persistence.models import ContinuityCapsuleRow
from atlas.runtime.conversation import turns_to_provider_messages
from atlas.transcript.models import Actor, TextBlock
from atlas.transcript.repository import TranscriptRepository
from sqlalchemy import func, select


class _FakeContinuityModel:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def complete_text(self, *, instructions: str, messages: list[dict[str, str]]) -> str:
        self.calls.append(messages[0]["content"])
        if "Prior handoff: (none" in messages[0]["content"]:
            return "Main topic: Normalizer. Open thread: improve Atlas cross-chat continuity."
        return "Main topic: Normalizer. Decision: use five-minute indexing. Open thread: continuity capsules."


@pytest.mark.asyncio
async def test_closed_chat_gets_rebuildable_handoff_and_active_chat_receives_it(pg_factory):
    async with pg_factory() as session:
        repo = TranscriptRepository(session)
        previous = await repo.get_or_create_active()
        await repo.rename_owner_chat(previous.id, "Atlas memory work")
        await repo.append_turn(previous.id, Actor.OWNER, [TextBlock(text="We are fixing cross-chat continuity")])
        await repo.append_turn(previous.id, Actor.ATLAS, [TextBlock(text="The indexing worker will run every five minutes")])
        current = await repo.create_owner_chat(title="Fresh chat")
        await repo.append_turn(current.id, Actor.OWNER, [TextBlock(text="Hey there")])
        await session.commit()

    model = _FakeContinuityModel()
    result = await ContinuityCapsuleService(pg_factory, model).run_once()
    assert result.capsules_created == 1
    assert result.failures == 0

    async with pg_factory() as session:
        capsules = list((await session.execute(
            select(ContinuityCapsuleRow).where(ContinuityCapsuleRow.transcript_id == previous.id)
        )).scalars())
        assert len(capsules) == 1
        assert capsules[0].start_sequence == 1
        assert capsules[0].end_sequence == 2
        context, count = await recent_continuity_context(session, current.id, limit=3)
        assert count == 1
        assert "Atlas memory work" in str(context)
        assert "Normalizer" in str(context)
        assert "canonical coverage sequences 1-2" in str(context)
        assert (await session.execute(
            select(func.count()).select_from(ContinuityCapsuleRow).where(
                ContinuityCapsuleRow.transcript_id == current.id
            )
        )).scalar_one() == 0


def test_continuity_context_is_explicitly_orientation_not_evidence() -> None:
    messages = turns_to_provider_messages([], continuity_context="Prior chat: Atlas memory work")
    assert len(messages) == 1
    assert messages[0]["role"] == "developer"
    assert "derived orientation" in messages[0]["content"]
    assert "not canonical evidence" in messages[0]["content"]
    assert "Prior chat: Atlas memory work" in messages[0]["content"]


@pytest.mark.asyncio
async def test_reopened_chat_gets_new_capsule_revision_from_prior_handoff(pg_factory):
    async with pg_factory() as session:
        repo = TranscriptRepository(session)
        first = await repo.get_or_create_active()
        await repo.rename_owner_chat(first.id, "Atlas")
        await repo.append_turn(first.id, Actor.OWNER, [TextBlock(text="Initial continuity work")])
        await repo.create_owner_chat(title="Second")
        await session.commit()

    model = _FakeContinuityModel()
    assert (await ContinuityCapsuleService(pg_factory, model).run_once()).capsules_created == 1

    async with pg_factory() as session:
        repo = TranscriptRepository(session)
        await repo.activate_owner_chat(first.id)
        await repo.append_turn(first.id, Actor.OWNER, [TextBlock(text="Add a five-minute worker")])
        await repo.append_turn(first.id, Actor.ATLAS, [TextBlock(text="Then add a small continuity handoff")])
        await repo.create_owner_chat(title="Third")
        await session.commit()

    result = await ContinuityCapsuleService(pg_factory, model).run_once()
    assert result.capsules_created >= 1
    async with pg_factory() as session:
        capsules = list((await session.execute(
            select(ContinuityCapsuleRow)
            .where(ContinuityCapsuleRow.transcript_id == first.id)
            .order_by(ContinuityCapsuleRow.revision)
        )).scalars())
        assert [item.revision for item in capsules] == [1, 2]
        assert capsules[1].start_sequence == capsules[0].start_sequence
        assert capsules[1].end_sequence == 3
        assert "Prior handoff: Main topic: Normalizer" in model.calls[-1]
