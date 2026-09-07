import asyncio
import base64
import json
from datetime import UTC, datetime, timedelta

import pytest
from atlas.actions.authority import AuthorityStore
from atlas.artifacts.store import ArtifactStore
from atlas.capabilities import CapabilityRuntime, OperationDescriptor
from atlas.config import Settings
from atlas.persistence.models import (
    ActionRow,
    ArtifactRow,
    OwnerAttentionRow,
    RunRow,
    ScheduledTaskRow,
    TurnRow,
)
from atlas.runtime.execution import RunExecutor
from atlas.runtime.observations import EvidenceStore
from atlas.runtime.recovery import RunInterrupted, recover_abandoned_runs
from atlas.transcript.repository import TranscriptRepository
from sqlalchemy import func, select


async def create_run(factory):
    async with factory() as session:
        transcript = await TranscriptRepository(session).get_or_create_active()
        run_id = await AuthorityStore(session).create_run(transcript_id=transcript.id, intent='fixture')
        await session.commit()
        return transcript.id, run_id


@pytest.mark.asyncio
async def test_exact_resource_is_stored_once_and_replayed_without_normalization(pg_factory, tmp_path):
    text = '<script>data, not instructions</script>\n  héllo 世界\n'
    artifacts = ArtifactStore(tmp_path / 'artifacts')
    transcript_id, run_id = await create_run(pg_factory)
    runtime = CapabilityRuntime()
    runtime.register(OperationDescriptor(id='fixture.read', capability_id='fixture', family='Fixture', description='fixture'),
        lambda args: {'resource': {'name': 'fixture.txt', 'media_type': 'text/plain', 'source': 'fixture',
            'data_base64': base64.b64encode(text.encode()).decode()}})
    executor = RunExecutor(pg_factory, runtime, artifacts, run_id=run_id, transcript_id=transcript_id)
    result = await executor.tool_handler('atlas_capability_call', {'operation_id': 'fixture.read', 'arguments': {}})
    resource = result['output']['resource']
    async with pg_factory() as session:
        turns = (await session.execute(select(TurnRow))).scalars().all()
        actions = (await session.execute(select(ActionRow))).scalars().all()
        assert 'data_base64' not in json.dumps([turn.blocks for turn in turns])
        assert 'data_base64' not in json.dumps([action.evidence for action in actions])
        assert (await session.execute(select(func.count()).select_from(ArtifactRow))).scalar_one() == 1
        store = EvidenceStore(session, artifacts)
        parts, offset = [], 0
        while True:
            chunk = await store.read(result['evidence_id'], artifact_id=resource['artifact_id'], offset=offset, limit=7)
            assert chunk['exact'] is True
            parts.append(chunk['text'])
            if chunk['next_offset'] is None:
                break
            offset = chunk['next_offset']
        assert ''.join(parts) == text
        acquired = await store.acquire(result['evidence_id'], resource['artifact_id'])
        assert base64.b64decode(acquired['resource']['data_base64']).decode() == text


@pytest.mark.asyncio
async def test_large_observation_uses_blob_and_exact_json_pointer(pg_factory, tmp_path):
    transcript_id, run_id = await create_run(pg_factory)
    async with pg_factory() as session:
        store = EvidenceStore(session, ArtifactStore(tmp_path))
        original = '<tag>  preserve me </tag>' * 2000
        evidence_id, _ = await store.record(transcript_id, operation='fixture.read', phase='succeeded',
            detail={'output': {'body': original}}, run_id=run_id)
        await session.commit()
        row = await session.get(TurnRow, evidence_id)
        assert len(json.dumps(row.blocks)) < 2000
        chunk = await store.read(str(evidence_id), pointer='/output/body', offset=4, limit=100)
        assert chunk['text'] == original[4:104]
        assert chunk['next_offset'] == 104


@pytest.mark.asyncio
async def test_abandoned_run_is_fenced_and_can_be_followed_by_new_owner_run(pg_factory, tmp_path):
    transcript_id, run_id = await create_run(pg_factory)
    async with pg_factory() as session:
        store = AuthorityStore(session)
        action_id = await store.begin_automatic_execution(run_id=run_id, operation='fixture.effect',
            arguments={}, summary='fixture', capability_id='fixture')
        run = await session.get(RunRow, run_id)
        run.heartbeat_at = datetime.now(UTC) - timedelta(minutes=10)
        await session.commit()
    artifacts = ArtifactStore(tmp_path)
    assert await recover_abandoned_runs(pg_factory, artifacts, stale_seconds=60) == 1
    assert await recover_abandoned_runs(pg_factory, artifacts, stale_seconds=60) == 0
    async with pg_factory() as session:
        run = await session.get(RunRow, run_id)
        assert run.inference_status == 'interrupted' and not run.inference_active
        assert run.status == 'uncertain'
        assert (await session.get(ActionRow, action_id)).status == 'uncertain'
        assert (await session.execute(select(OwnerAttentionRow).where(OwnerAttentionRow.run_id == run_id))).scalars().all()
        new_id = await AuthorityStore(session).create_run(transcript_id=transcript_id, intent='continue')
        await session.commit()
        assert new_id != run_id
    executor = RunExecutor(pg_factory, CapabilityRuntime(), artifacts, run_id=run_id, transcript_id=transcript_id)
    with pytest.raises(RunInterrupted):
        await executor.tool_handler('atlas_capability_search', {'query': 'anything'})


@pytest.mark.asyncio
async def test_live_heartbeat_is_not_recovered(pg_factory, tmp_path):
    await create_run(pg_factory)
    assert await recover_abandoned_runs(pg_factory, ArtifactStore(tmp_path), stale_seconds=60) == 0


@pytest.mark.asyncio
async def test_schedule_claim_survives_interruption_without_losing_occurrence(pg_factory, tmp_path, monkeypatch):
    from atlas.schedules import runner
    monkeypatch.setattr(runner, 'get_session_factory', lambda: pg_factory)
    settings = Settings(artifact_dir=tmp_path)
    async with pg_factory() as session:
        task = ScheduledTaskRow(title='Once', prompt='Original intent', schedule_kind='once',
            schedule_value='2020-01-01T00:00:00+00:00', timezone='UTC', enabled=True,
            next_run_at=datetime.now(UTC) - timedelta(seconds=1))
        session.add(task)
        await session.commit()
        task_id = task.id
    assert await runner.claim_due(settings) == 1
    assert await runner.claim_due(settings) == 0
    async with pg_factory() as session:
        task = await session.get(ScheduledTaskRow, task_id)
        run = (await session.execute(select(RunRow).where(RunRow.schedule_id == task_id))).scalar_one()
        assert not task.enabled
        assert run.status == 'queued' and run.inference_status == 'queued'
        task.prompt = 'Changed later'
        await session.commit()
        assert run.trigger_snapshot['prompt'] == 'Original intent'
        assert (await session.execute(select(TurnRow).where(TurnRow.transcript_id == run.transcript_id))).scalars().all()


@pytest.mark.asyncio
async def test_slow_executor_does_not_block_event_loop():
    import threading
    entered, release = threading.Event(), threading.Event()
    runtime = CapabilityRuntime()
    def slow(args):
        entered.set()
        assert release.wait(timeout=2)
        return {'ok': True}
    runtime.register(OperationDescriptor(id='fixture.slow', capability_id='fixture', family='Fixture', description='fixture'), slow)
    call = asyncio.create_task(runtime.call('fixture.slow', {}))
    try:
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(0.005)
        assert entered.is_set()
        assert not call.done()
    finally:
        release.set()
    assert (await call).status == 'succeeded'


@pytest.mark.asyncio
@pytest.mark.parametrize('delta', [None, {'runtime': {'status': 'complete'}}, {'status': 'complete'}])
async def test_foreground_completion_requires_explicit_valid_delta(pg_factory, tmp_path, monkeypatch, delta):
    import atlas.api.app as app_module
    key = tmp_path / 'fake-key'
    key.write_text('test-not-a-real-credential')
    class Provider:
        def __init__(self, **kwargs):
            pass
        async def count_input_tokens(self, **kwargs):
            return 100
        async def stream_text(self, **kwargs):
            if delta is not None:
                await kwargs['task_state_handler'](delta)
            yield 'Answer'
    monkeypatch.setattr(app_module, 'get_session_factory', lambda: pg_factory)
    monkeypatch.setattr(app_module, 'OpenAIProvider', Provider)
    monkeypatch.setattr(app_module.settings, 'openai_api_key_file', key)
    monkeypatch.setattr(app_module, 'artifact_store', ArtifactStore(tmp_path / 'artifacts'))
    response = await app_module.stream_conversation(app_module.ChatRequest(text='Keep doing the task'))
    async for _ in response.body_iterator:
        pass
    async with pg_factory() as session:
        transcript = await TranscriptRepository(session).get_or_create_active()
        assert transcript.active_task_state['status'] == ('complete' if delta == {'status': 'complete'} else 'active')
        assert (await session.execute(select(RunRow))).scalar_one().inference_active is False


@pytest.mark.asyncio
async def test_owner_attachment_reference_survives_file_change(pg_factory, tmp_path, monkeypatch):
    import atlas.api.app as app_module
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    attached = workspace / 'note.txt'
    attached.write_text('Original attached content')
    key = tmp_path / 'fake-key'
    key.write_text('test-not-a-real-credential')
    class Provider:
        def __init__(self, **kwargs): pass
        async def count_input_tokens(self, **kwargs): return 100
        async def stream_text(self, **kwargs):
            assert any('evidence.resource.acquire' in str(item) for item in kwargs['messages'])
            yield 'Answer'
    artifacts = ArtifactStore(tmp_path / 'artifacts')
    monkeypatch.setattr(app_module, 'get_session_factory', lambda: pg_factory)
    monkeypatch.setattr(app_module, 'OpenAIProvider', Provider)
    monkeypatch.setattr(app_module.settings, 'openai_api_key_file', key)
    monkeypatch.setattr(app_module.settings, 'workspace_root', workspace)
    monkeypatch.setattr(app_module, 'artifact_store', artifacts)
    response = await app_module.stream_conversation(app_module.ChatRequest(text='Read this', attachments=['note.txt']))
    attached.write_text('Changed after attachment')
    async for _ in response.body_iterator: pass
    async with pg_factory() as session:
        owner = (await session.execute(select(TurnRow).where(TurnRow.actor == 'owner'))).scalar_one()
        reference = next(block for block in owner.blocks if block['type'] == 'artifact_ref')
        result = await EvidenceStore(session, artifacts).read(str(owner.id), artifact_id=reference['artifact_id'])
        assert result['text'] == 'Original attached content'


@pytest.mark.asyncio
async def test_cancelled_foreground_retains_task_and_releases_slot(pg_factory, tmp_path, monkeypatch):
    import atlas.api.app as app_module
    key = tmp_path / 'fake-key'
    key.write_text('test-not-a-real-credential')
    class Provider:
        def __init__(self, **kwargs): pass
        async def count_input_tokens(self, **kwargs): return 100
        async def stream_text(self, **kwargs):
            await kwargs['task_state_handler']({'objective': 'Keep objective', 'next_step': 'Continue here'})
            yield 'Partial'
            raise asyncio.CancelledError
    monkeypatch.setattr(app_module, 'get_session_factory', lambda: pg_factory)
    monkeypatch.setattr(app_module, 'OpenAIProvider', Provider)
    monkeypatch.setattr(app_module.settings, 'openai_api_key_file', key)
    monkeypatch.setattr(app_module, 'artifact_store', ArtifactStore(tmp_path / 'artifacts'))
    response = await app_module.stream_conversation(app_module.ChatRequest(text='Continue'))
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as conflict:
        await app_module.stream_conversation(app_module.ChatRequest(text='Second tab'))
    assert conflict.value.status_code == 409
    with pytest.raises(asyncio.CancelledError):
        async for _ in response.body_iterator: pass
    async with pg_factory() as session:
        transcript = await TranscriptRepository(session).get_or_create_active()
        assert transcript.active_task_state['semantic']['objective'] == 'Keep objective'
        assert transcript.active_task_state['semantic']['next_step'] == 'Continue here'
        run = (await session.execute(select(RunRow))).scalar_one()
        assert run.status == 'interrupted' and not run.inference_active
        owners = (await session.execute(select(TurnRow).where(TurnRow.actor == 'owner'))).scalars().all()
        assert len(owners) == 1
        from atlas.actions.authority import ProposalIntegrityError
        with pytest.raises(ProposalIntegrityError):
            await AuthorityStore(session).finish_run(run.id)


@pytest.mark.asyncio
async def test_late_effect_result_adds_evidence_without_rewriting_uncertainty(pg_factory, tmp_path):
    from atlas.actions.models import ActionStatus
    from atlas.capabilities.models import CapabilityCallResult
    transcript_id, run_id = await create_run(pg_factory)
    async with pg_factory() as session:
        store = AuthorityStore(session)
        action_id = await store.begin_automatic_execution(run_id=run_id, operation='fixture.effect',
            arguments={}, summary='fixture', capability_id='fixture')
        await store.complete(action_id, status=ActionStatus.UNCERTAIN)
        await session.commit()
    executor = RunExecutor(pg_factory, CapabilityRuntime(), ArtifactStore(tmp_path), run_id=run_id, transcript_id=transcript_id)
    await executor.finish_action(action_id, CapabilityCallResult(operation_id='fixture.effect', status='succeeded', output={'id': 'external1'}), {})
    async with pg_factory() as session:
        action = await session.get(ActionRow, action_id)
        assert action.status == 'uncertain'
        assert action.evidence['late_result_evidence_id']
        assert (await session.get(RunRow, run_id)).status == 'uncertain'


@pytest.mark.asyncio
async def test_queued_schedule_executes_snapshot_once(pg_factory, tmp_path, monkeypatch):
    from atlas.schedules import runner
    key = tmp_path / 'fake-key'
    key.write_text('test-not-a-real-credential')
    settings = Settings(artifact_dir=tmp_path / 'artifacts', openai_api_key_file=key)
    seen = []
    class Provider:
        def __init__(self, **kwargs): pass
        async def stream_text(self, **kwargs):
            seen.extend(kwargs['messages'])
            yield 'Scheduled answer'
    monkeypatch.setattr(runner, 'get_session_factory', lambda: pg_factory)
    monkeypatch.setattr(runner, 'OpenAIProvider', Provider)
    async with pg_factory() as session:
        task = ScheduledTaskRow(title='Once', prompt='Original snapshot intent', schedule_kind='once',
            schedule_value='2020-01-01T00:00:00+00:00', timezone='UTC', enabled=True,
            next_run_at=datetime.now(UTC) - timedelta(seconds=1))
        session.add(task)
        await session.commit()
        task_id = task.id
    assert await runner.claim_due(settings) == 1
    async with pg_factory() as session:
        task = await session.get(ScheduledTaskRow, task_id)
        task.prompt = 'New mutable intent'
        await session.commit()
    assert await runner.run_due_once(settings, CapabilityRuntime()) == 1
    assert await runner.run_due_once(settings, CapabilityRuntime()) == 0
    assert len(seen) == 1 and 'Original snapshot intent' in seen[0]['content']
    assert 'New mutable intent' not in seen[0]['content']
    async with pg_factory() as session:
        assert (await session.get(ScheduledTaskRow, task_id)).last_status == 'succeeded'


@pytest.mark.asyncio
async def test_exact_task_paging_preserves_revision_and_all_action_refs(pg_factory, tmp_path):
    from atlas.runtime.task_state import new_task_state
    transcript_id, _run_id = await create_run(pg_factory)
    async with pg_factory() as session:
        state = new_task_state('Continue')
        state['runtime']['pending_actions'] = [{'action_id': str(index)} for index in range(1000)]
        state = await TranscriptRepository(session).update_active_task_state(transcript_id, state, expected_revision=0)
        await session.commit()
        store = EvidenceStore(session, ArtifactStore(tmp_path))
        parts, offset = [], 0
        while True:
            page = await store.task_read(state['task_id'], expected_revision=state['revision'], offset=offset, limit=8000)
            parts.append(page['text'])
            if page['next_offset'] is None: break
            offset = page['next_offset']
        assert json.loads(''.join(parts)) == state
        with pytest.raises(ValueError, match='changed'):
            await store.task_read(state['task_id'], expected_revision=0)


@pytest.mark.asyncio
async def test_invalid_non_object_arguments_never_dispatch(pg_factory, tmp_path):
    from atlas.capabilities import EffectKind
    transcript_id, run_id = await create_run(pg_factory)
    runtime = CapabilityRuntime()
    dispatched = []
    runtime.register(OperationDescriptor(id='fixture.effect', capability_id='fixture', family='Fixture',
        description='fixture', effect=EffectKind.CREATE), lambda args: dispatched.append(args))
    executor = RunExecutor(pg_factory, runtime, ArtifactStore(tmp_path), run_id=run_id, transcript_id=transcript_id)
    result = await executor.tool_handler('atlas_capability_call', {'operation_id': 'fixture.effect', 'arguments': []})
    assert result['status'] == 'failed'
    assert result['output']['failure_phase'] == 'before_dispatch'
    assert dispatched == []
