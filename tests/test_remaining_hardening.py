import json
import zipfile
from unittest.mock import patch

import pytest
from atlas.actions.authority import AuthorityStore, ProposalIntegrityError
from atlas.capabilities import (
    AuthorityMode,
    CapabilityRuntime,
    EffectKind,
    OperationDescriptor,
)
from atlas.persistence.models import ActionRow, OwnerAttentionRow
from atlas.providers.openai import ContextBudgetExceeded, OpenAIProvider
from atlas.registry.models import (
    CapabilityAvailability,
    CapabilityEntry,
    CapabilitySource,
)
from atlas.registry.repository import RegistryRepository
from atlas.storage import ProjectFolderService
from atlas.storage.changes import ProjectChanges
from atlas.transcript.models import Actor, TextBlock
from atlas.transcript.repository import TranscriptRepository
from sqlalchemy import select


@pytest.mark.asyncio
async def test_approval_reads_verified_canonical_payload_and_checks_displayed_version(pg_factory):
    async with pg_factory() as session:
        transcript = await TranscriptRepository(session).get_or_create_active()
        store = AuthorityStore(session)
        run_id = await store.create_run(transcript_id=transcript.id, intent='fixture')
        args = {'to': 'to@example.test', 'bcc': 'hidden@example.test', 'body': 'Full message', 'subject': 'Fixture'}
        action_id = await store.prepare_proposal(run_id=run_id, operation='gmail.message.send', arguments=args, title='fixture')
        await session.commit()
        attention = (await session.execute(select(OwnerAttentionRow))).scalar_one()
        attention.detail = {'arguments': {'to': 'wrong@example.test'}}
        await session.commit()
        detail = (await store.pending())[0]['detail']
        assert detail['arguments'] == args and detail['reviewable'] is True
        with pytest.raises(ProposalIntegrityError, match='changed after review'):
            await store.begin_execution(await session.get(ActionRow, action_id), reviewed_target_hash='0' * 64)
        await session.rollback()
        await store.begin_execution(await session.get(ActionRow, action_id), reviewed_target_hash=detail['reviewed_target_hash'])
        await session.commit()
        assert (await session.get(ActionRow, action_id)).status == 'executing'


@pytest.mark.asyncio
async def test_owner_enablement_survives_startup_and_guards_discovery_and_approval(pg_factory):
    entry = CapabilityEntry(id='fixture', family='Fixture', description='Fixture', source=CapabilitySource.ATLAS,
        enabled=True, availability=CapabilityAvailability.AVAILABLE)
    async with pg_factory() as session:
        repository = RegistryRepository(session)
        await repository.upsert(entry)
        assert await repository.enabled_ids() == set()
        await repository.set_enabled('fixture', True)
        await session.commit()
        await repository.upsert(entry.model_copy(update={'enabled': False}))
        await session.commit()
        assert await repository.enabled_ids() == {'fixture'}
    runtime = CapabilityRuntime()
    effects, proposals = [], []
    runtime.register(OperationDescriptor(id='fixture.effect', capability_id='fixture', family='Fixture', description='Fixture',
        effect=EffectKind.CREATE, authority=AuthorityMode.APPROVAL_REQUIRED), lambda args: effects.append(args))
    async def policy():
        async with pg_factory() as session:
            return await RegistryRepository(session).enabled_ids()
    async def propose(*args):
        proposals.append(args)
        return 'proposal'
    runtime.policy_reader = policy
    assert (await runtime.search_cards_current('fixture'))
    assert (await runtime.call('fixture.effect', {}, proposal_sink=propose)).status == 'approval_required'
    async with pg_factory() as session:
        await RegistryRepository(session).set_enabled('fixture', False)
        await RegistryRepository(session).upsert(entry)
        await session.commit()
    assert await runtime.search_cards_current('fixture') == []
    assert await runtime.compact_index_current() == []
    assert await runtime.descriptor_current('fixture.effect') is None
    assert (await runtime.call('fixture.effect', {}, approval_granted=True)).status == 'forbidden'
    assert effects == [] and len(proposals) == 1


@pytest.mark.parametrize('operation', ['apply', 'move', 'delete'])
def test_staged_changes_preserve_live_owner_save_at_final_boundary(tmp_path, operation):
    project = tmp_path / 'Demo'
    project.mkdir()
    owner_file = project / 'note.txt'
    owner_file.write_text('Original\n')
    projects = ProjectFolderService(tmp_path, 'Projects', tmp_path / 'private')
    changes = ProjectChanges(projects)
    preview = projects.preview_file('Demo/note.txt', 'Atlas proposal\n')
    original_save = changes._save
    def owner_save(*args, **kwargs):
        owner_file.write_text('New owner edit\n')
        return original_save(*args, **kwargs)
    with patch.object(changes, '_save', owner_save):
        if operation == 'apply':
            result = changes.apply_file('Demo/note.txt', 'Atlas proposal\n', preview['expected_sha256'], preview['change_token'])
        elif operation == 'move':
            result = changes.move_file('Demo/note.txt', 'Demo/new.txt', preview['expected_sha256'])
        else:
            result = changes.delete_file('Demo/note.txt', preview['expected_sha256'])
    assert owner_file.read_text() == 'New owner edit\n'
    assert not (project / 'new.txt').exists()
    assert result['status'] == 'staged' and result['live_files_modified'] is False
    with zipfile.ZipFile(changes.root / f"{result['change_id']}.zip") as bundle:
        assert bundle.read('original') == b'Original\n'
        if operation == 'apply':
            assert bundle.read('proposed') == b'Atlas proposal\n'
        assert json.loads(bundle.read('manifest.json'))['operation'] == ('update' if operation == 'apply' else operation)


@pytest.mark.asyncio
async def test_history_queries_are_bounded_and_preserve_latest_owner(pg_factory):
    async with pg_factory() as session:
        repository = TranscriptRepository(session)
        transcript = await repository.get_or_create_active()
        owner = await repository.append_turn(transcript.id, Actor.OWNER, [TextBlock(text='Current request')])
        for index in range(600):
            await repository.append_turn(transcript.id, Actor.TOOL, [TextBlock(text=f'fixture {index}')])
        await session.commit()
        latest = await repository.list_turns(transcript.id)
        older = await repository.list_turns(transcript.id, before_sequence=latest[0].sequence)
        assert len(latest) == len(older) == 200
        assert older[-1].sequence < latest[0].sequence
        window = await repository.list_recent_turns(transcript.id)
        assert len(window) == 500 and window[0].id == owner.id
        assert window[-1].sequence == 601


@pytest.mark.asyncio
async def test_loop_budget_keeps_task_core_and_exact_evidence_locator():
    provider = object.__new__(OpenAIProvider)
    provider.input_token_budget = 650
    async def count(**kwargs):
        return len(json.dumps(kwargs['messages']))
    provider.count_input_tokens = count
    base = [{'role': 'developer', 'content': 'Protected active-task checkpoint. objective: Finish; next step: inspect'},
        {'role': 'user', 'content': 'Continue'}]
    rounds = [{'items': [{'type': 'function_call', 'name': 'fixture', 'arguments': 'x' * 3000},
        {'type': 'function_call_output', 'output': 'y' * 3000}], 'evidence_ids': ['exact-observation']}]
    fitted = await provider._fit_loop_input('Fixture', base, rounds)
    assert await count(messages=fitted) <= 650
    assert fitted[:2] == base
    assert 'exact-observation' in fitted[-1]['content']
    assert not any(item.get('type') == 'function_call_output' for item in fitted)
    with pytest.raises(ContextBudgetExceeded):
        await provider._fit_loop_input('Fixture', [{'role': 'developer', 'content': 'x' * 1000}], [])


@pytest.mark.asyncio
async def test_native_web_is_checked_again_on_each_provider_request():
    provider = object.__new__(OpenAIProvider)
    enabled = {'openai.web'}
    async def policy(): return set(enabled)
    provider.capability_policy = policy
    assert any(tool['type'] == 'web_search' for tool in await provider._current_tools())
    enabled.clear()
    assert not any(tool['type'] == 'web_search' for tool in await provider._current_tools())
    assert await provider._current_tools(controls=False) == []


@pytest.mark.asyncio
async def test_production_factory_stages_mutations_and_denies_unknown_policy(tmp_path, monkeypatch):
    from atlas.capabilities.factory import build_capability_runtime
    from atlas.config import Settings
    from atlas.registry.service import build_phase0_registry
    from sqlalchemy.exc import OperationalError

    class UnavailableDatabase:
        async def __aenter__(self): raise OperationalError('fixture', None, None)
        async def __aexit__(self, *args): pass
    monkeypatch.setattr('atlas.capabilities.factory.get_session_factory', lambda: UnavailableDatabase)
    settings = Settings(projects_root=tmp_path / 'projects', project_checkpoint_root=tmp_path / 'changes',
        gws_config_dir=tmp_path / 'unconfigured', openai_api_key_file=None, github_token_file=None)
    project = settings.projects_root / 'Demo'
    project.mkdir(parents=True)
    (project / 'note.txt').write_text('Owner original')
    runtime = build_capability_runtime(settings, build_phase0_registry(settings))
    denied = await runtime.call('storage.projects.preview', {'path': 'Demo/note.txt', 'content': 'Proposed'})
    assert denied.status == 'forbidden'
    async def enabled(): return {'atlas.project_folders'}
    runtime.policy_reader = enabled
    preview = (await runtime.call('storage.projects.preview', {'path': 'Demo/note.txt', 'content': 'Proposed'})).output
    calls = [
        ('storage.projects.apply', {'path': 'Demo/note.txt', 'content': 'Proposed', 'expected_sha256': preview['expected_sha256'], 'change_token': preview['change_token']}),
        ('storage.projects.move', {'source_path': 'Demo/note.txt', 'target_path': 'Demo/moved.txt', 'expected_sha256': preview['expected_sha256']}),
        ('storage.projects.delete', {'path': 'Demo/note.txt', 'expected_sha256': preview['expected_sha256']}),
    ]
    for operation, arguments in calls:
        result = await runtime.call(operation, arguments, approval_granted=True)
        assert result.status == 'succeeded' and result.output['status'] == 'staged'
        assert (project / 'note.txt').read_text() == 'Owner original'
        assert not (project / 'moved.txt').exists()
    for path in ('Demo/.env', 'Demo/credentials/token.txt'):
        assert (await runtime.call('storage.projects.preview', {'path': path, 'content': 'secret'})).status == 'failed'


@pytest.mark.asyncio
async def test_real_provider_loop_recounts_and_revokes_native_web_before_dispatch():
    from types import SimpleNamespace
    class Call:
        type, name, arguments, call_id = 'function_call', 'atlas_capability_call', '{"operation_id":"fixture.read","arguments":{}}', 'c1'
        def model_dump(self, **kwargs):
            return {'type': self.type, 'name': self.name, 'arguments': self.arguments, 'call_id': self.call_id}
    enabled, requests, counts = {'openai.web'}, [], []
    provider = object.__new__(OpenAIProvider)
    provider.model, provider.capability_call_limit, provider.capability_completion_reserve = 'fixture', 16, 2
    provider.input_token_budget = 2600
    async def policy(): return set(enabled)
    async def count(**kwargs):
        size = len(json.dumps(kwargs['input']))
        counts.append(size)
        # Simulate owner revocation while the count request is in flight.
        enabled.clear()
        return SimpleNamespace(input_tokens=size)
    async def create(**kwargs):
        requests.append(kwargs)
        assert len(json.dumps(kwargs['input'])) <= provider.input_token_budget
        assert not any(tool['type'] == 'web_search' for tool in kwargs['tools'])
        return SimpleNamespace(id='response', status='completed', output=[Call()] if len(requests) == 1 else [], output_text='' if len(requests) == 1 else 'Done')
    async def tool(*args): return {'evidence_id': 'exact-1', 'output': {'text': 'X' * 30000}, 'status': 'succeeded'}
    async def observe(payload): return 'provider-evidence'
    async def checkpoint(): return {'role': 'developer', 'content': 'Protected active-task checkpoint. Keep objective and next step.'}
    provider.capability_policy = policy
    provider.client = SimpleNamespace(responses=SimpleNamespace(create=create, input_tokens=SimpleNamespace(count=count)))
    chunks = [chunk async for chunk in provider.stream_text(instructions='Fixture', messages=[{'role': 'user', 'content': 'Continue'}],
        tool_handler=tool, observation_handler=observe, checkpoint_reader=checkpoint)]
    assert chunks == ['Done'] and len(requests) == 2 and len(counts) > 2
    assert any('exact-1' in str(item) for item in requests[1]['input'])
    assert requests[1]['input'][0]['content'].startswith('Protected active-task checkpoint.')


@pytest.mark.asyncio
async def test_enablement_change_never_dispatches_without_durable_action(pg_factory, tmp_path):
    from atlas.artifacts.store import ArtifactStore
    from atlas.runtime.execution import RunExecutor
    async with pg_factory() as session:
        transcript = await TranscriptRepository(session).get_or_create_active()
        run_id = await AuthorityStore(session).create_run(transcript_id=transcript.id, intent='fixture')
        await session.commit()
    runtime = CapabilityRuntime()
    checks, effects = [], []
    async def policy():
        checks.append(True)
        return set() if len(checks) == 1 else {'fixture'}
    async def effect(args):
        async with pg_factory() as session:
            executing = (await session.execute(select(ActionRow).where(ActionRow.run_id == run_id, ActionRow.status == 'executing'))).scalars().all()
            effects.append(bool(executing))
        return {'done': True}
    runtime.policy_reader = policy
    runtime.register(OperationDescriptor(id='fixture.write', capability_id='fixture', family='Fixture', description='Fixture',
        effect=EffectKind.UPDATE, authority=AuthorityMode.AUTO), effect)
    executor = RunExecutor(pg_factory, runtime, ArtifactStore(tmp_path), run_id=run_id, transcript_id=transcript.id)
    await executor.tool_handler('atlas_capability_call', {'operation_id': 'fixture.write', 'arguments': {}})
    assert all(effects), 'A policy transition dispatched before committing an executing action'
    await executor.tool_handler('atlas_capability_call', {'operation_id': 'fixture.write', 'arguments': {}})
    assert effects and all(effects)


@pytest.mark.asyncio
async def test_disabled_artifact_upload_has_no_file_or_database_effect(pg_factory, tmp_path, monkeypatch):
    import io

    import atlas.api.app as app_module
    from atlas.artifacts.store import ArtifactStore
    from atlas.persistence.models import ArtifactRow
    from fastapi import HTTPException, UploadFile
    async def disabled(): return set()
    root = tmp_path / 'artifacts'
    monkeypatch.setattr(app_module.capability_runtime, 'enabled_capabilities', disabled)
    monkeypatch.setattr(app_module, 'artifact_store', ArtifactStore(root))
    async with pg_factory() as session:
        with pytest.raises(HTTPException) as denied:
            await app_module.upload_artifact(UploadFile(filename='fixture.txt', file=io.BytesIO(b'Owner bytes')), session)
        assert denied.value.status_code == 403
        assert (await session.execute(select(ArtifactRow))).scalars().all() == []
    assert not root.exists() or list(root.rglob('*')) == []

    async def enabled(): return {'atlas.artifacts'}
    monkeypatch.setattr(app_module.capability_runtime, 'enabled_capabilities', enabled)
    async with pg_factory() as session:
        artifact = await app_module.upload_artifact(UploadFile(filename='fixture.txt', file=io.BytesIO(b'Owner bytes')), session)
        assert (root / artifact.storage_key).read_bytes() == b'Owner bytes'
        assert len((await session.execute(select(ArtifactRow))).scalars().all()) == 1
