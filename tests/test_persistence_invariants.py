import asyncio

import pytest
from atlas.actions.authority import AuthorityStore, ForegroundBusy
from atlas.runtime.task_state import (
    TaskStateDelta,
    merge_semantic_delta,
    new_task_state,
    record_runtime_event,
)
from atlas.transcript.models import Actor, TextBlock
from atlas.transcript.repository import TaskStateConflict, TranscriptRepository


async def owner_transcript(factory):
    async with factory() as session:
        transcript = await TranscriptRepository(session).get_or_create_active()
        await session.commit()
        return transcript


@pytest.mark.asyncio
async def test_concurrent_active_owner_creation_returns_one_transcript(pg_factory):
    one, two = await asyncio.gather(owner_transcript(pg_factory), owner_transcript(pg_factory))
    assert one.id == two.id


@pytest.mark.asyncio
async def test_canonical_sequence_preserves_transaction_append_order(pg_factory):
    transcript = await owner_transcript(pg_factory)
    async with pg_factory() as session:
        repo = TranscriptRepository(session)
        for i in range(20):
            await repo.append_turn(transcript.id, Actor.OWNER, [TextBlock(text=str(i))])
        await session.commit()
        turns = await repo.list_turns(transcript.id)
    assert [turn.sequence for turn in turns] == list(range(1, 21))
    assert [turn.blocks[0].text for turn in turns] == [str(i) for i in range(20)]


@pytest.mark.asyncio
async def test_stale_checkpoint_write_is_rejected(pg_factory):
    transcript = await owner_transcript(pg_factory)
    async with pg_factory() as session:
        repo = TranscriptRepository(session)
        state = await repo.update_active_task_state(transcript.id, new_task_state('owner'), expected_revision=0)
        await session.commit()
        original_id = state['task_id']
        updated = await repo.update_active_task_state(transcript.id,
            merge_semantic_delta(state, TaskStateDelta(objective='new')), expected_revision=1)
        await session.commit()
        assert updated['revision'] == 2 and updated['task_id'] == original_id
        with pytest.raises(TaskStateConflict):
            await repo.update_active_task_state(transcript.id, state, expected_revision=1)
        await session.rollback()
        current = await repo.get_active_task_state(transcript.id)
        assert current['semantic']['objective'] == 'new'


@pytest.mark.asyncio
async def test_concurrent_task_events_are_both_retained(pg_factory):
    transcript = await owner_transcript(pg_factory)
    async def record(name):
        async with pg_factory() as session:
            await TranscriptRepository(session).mutate_active_task_state(transcript.id, lambda state:
                record_runtime_event(state, operation=name, phase='succeeded', evidence_id=name))
            await session.commit()
    await asyncio.gather(record('one'), record('two'))
    async with pg_factory() as session:
        state = await TranscriptRepository(session).get_active_task_state(transcript.id)
    assert {event['operation'] for event in state['runtime']['recent_events']} == {'one', 'two'}
    assert state['revision'] == 2


@pytest.mark.asyncio
async def test_only_one_foreground_inference_can_claim_transcript(pg_factory):
    transcript = await owner_transcript(pg_factory)
    async def claim():
        async with pg_factory() as session:
            try:
                run_id = await AuthorityStore(session).create_run(transcript_id=transcript.id, intent='test')
                await session.commit()
                return run_id
            except ForegroundBusy:
                return None
    results = await asyncio.gather(claim(), claim())
    assert sum(result is not None for result in results) == 1


@pytest.mark.asyncio
async def test_sibling_success_cannot_hide_uncertainty(pg_factory):
    from atlas.actions.models import ActionStatus
    from atlas.persistence.models import RunRow
    transcript = await owner_transcript(pg_factory)
    async with pg_factory() as session:
        store = AuthorityStore(session)
        run_id = await store.create_run(transcript_id=transcript.id, intent='two effects')
        first = await store.begin_automatic_execution(run_id=run_id, operation='test.first', arguments={}, summary='first', capability_id='test')
        second = await store.begin_automatic_execution(run_id=run_id, operation='test.second', arguments={}, summary='second', capability_id='test')
        await session.commit()
        await store.complete(first, status=ActionStatus.UNCERTAIN)
        await session.commit()
        await store.complete(second, status=ActionStatus.SUCCEEDED)
        await store.finish_run(run_id)
        await session.commit()
        assert (await session.get(RunRow, run_id)).status == 'uncertain'


@pytest.mark.asyncio
async def test_stale_cancel_cannot_overwrite_execution(pg_factory):
    from atlas.actions.authority import ProposalIntegrityError
    from atlas.persistence.models import ActionRow
    transcript = await owner_transcript(pg_factory)
    async with pg_factory() as session:
        store = AuthorityStore(session)
        run_id = await store.create_run(transcript_id=transcript.id, intent='approval')
        action_id = await store.prepare_proposal(run_id=run_id, operation='test.create', arguments={}, title='test')
        await session.commit()
    async with pg_factory() as cancelling, pg_factory() as approving:
        stale = await cancelling.get(ActionRow, action_id)
        await AuthorityStore(approving).begin_execution(await approving.get(ActionRow, action_id))
        await approving.commit()
        with pytest.raises(ProposalIntegrityError):
            await AuthorityStore(cancelling).cancel(stale)
        await cancelling.rollback()
    async with pg_factory() as session:
        assert (await session.get(ActionRow, action_id)).status == 'executing'
