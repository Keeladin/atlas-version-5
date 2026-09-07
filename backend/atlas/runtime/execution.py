"""Shared deterministic dispatch and evidence handling for foreground/scheduled runs."""
from typing import Any
from uuid import UUID

from atlas.actions.authority import AuthorityStore
from atlas.actions.models import ActionStatus
from atlas.capabilities import AuthorityMode, EffectKind
from atlas.persistence.models import ActionRow, OwnerAttentionRow
from atlas.runtime.observations import EvidenceStore
from atlas.runtime.recovery import require_live_run


def action_status_for_result(result) -> ActionStatus:
    phase = result.output.get('failure_phase') if isinstance(result.output, dict) else None
    if result.status == 'succeeded':
        return ActionStatus.SUCCEEDED
    if result.status in {'forbidden', 'unavailable'} or phase == 'before_dispatch':
        return ActionStatus.FAILED
    return ActionStatus.UNCERTAIN


def external_effect_id(output: Any) -> str | None:
    if isinstance(output, dict):
        for key in ('id', 'messageId', 'message_id', 'eventId', 'event_id'):
            value = output.get(key)
            if isinstance(value, (str, int)) and str(value):
                return str(value)
    return None


class RunExecutor:
    def __init__(self, factory, runtime, artifacts, *, run_id: UUID, transcript_id: UUID, checkpoint: bool = True):
        self.factory, self.runtime, self.artifacts = factory, runtime, artifacts
        self.run_id, self.transcript_id, self.checkpoint = run_id, transcript_id, checkpoint

    async def record(self, session, operation, phase, detail, *, action_id=None, arguments=None, trust='external'):
        return await EvidenceStore(session, self.artifacts).record(self.transcript_id,
            operation=operation, phase=phase, detail=detail, run_id=self.run_id,
            action_id=action_id, arguments=arguments, checkpoint=self.checkpoint and operation != "evidence.task.read", trust=trust)

    async def finish_action(self, action_id, result, arguments):
        status = action_status_for_result(result)
        async with self.factory() as session:
            store = AuthorityStore(session)
            # Run -> transcript is the common lock order for action evidence.
            await store._lock_run(self.run_id)
            action = await session.get(ActionRow, action_id, populate_existing=True)
            late = action.status != 'executing'
            evidence_id, frozen = await self.record(session, result.operation_id, 'late_result' if late else status.value,
                result.model_dump(mode='json'), action_id=action_id, arguments=arguments)
            if late:
                action.evidence = {**action.evidence, 'late_result_evidence_id': str(evidence_id)}
            else:
                await store.complete(action_id, status=status,
                    result={'status': result.status, 'evidence_id': str(evidence_id)},
                    external_id=external_effect_id(result.output))
            if (not late and result.status == 'succeeded' and result.operation_id.startswith('storage.projects.')
                    and isinstance(result.output, dict) and result.output.get('status') == 'staged'):
                change_id = UUID(result.output['change_id'])
                session.add(OwnerAttentionRow(run_id=self.run_id, state='staged_change', title='Project change ready to review',
                    detail={'message': 'Live project files are unchanged. Download the bundle and integrate it through your editor or version-control workflow.',
                        'download_url': f'/api/project-changes/{change_id}', 'path': result.output.get('path')}))
            await session.commit()
            frozen = {**frozen, 'action_status': action.status}
            if late:
                frozen = {**frozen, 'status': action.status, 'late_reported_status': result.status}
        return evidence_id, frozen

    async def tool_handler(self, name: str, arguments: dict[str, Any]) -> dict:
        async with self.factory() as session:
            await require_live_run(session, self.run_id)
            await session.commit()
        if name == 'atlas_capability_search':
            query = str(arguments.get('query') or '')
            result = {'operations': await self.runtime.search_cards_current(query, int(arguments.get('limit') or 8))}
            async with self.factory() as session:
                evidence_id, _ = await EvidenceStore(session, self.artifacts).record(self.transcript_id,
                    operation=name, phase='searched', detail={'query': query, 'result': result},
                    run_id=self.run_id, checkpoint=False, trust='internal')
                await session.commit()
            return {**result, 'evidence_id': str(evidence_id)}
        if name != 'atlas_capability_call':
            return {'status': 'unavailable', 'message': 'Unknown Atlas capability control tool.'}
        operation = str(arguments.get('operation_id') or '')
        args = arguments.get('arguments', {})
        # Effect classification must remain stable across owner policy changes.
        # Dispatch checks current permission separately after action identity commits.
        descriptor = self.runtime.descriptor(operation)
        validation_error = self.runtime.validate_arguments(operation, args)
        action_id = None
        proposal_evidence = None

        async def propose(item, prepared_args):
            nonlocal proposal_evidence
            async with self.factory() as session:
                await require_live_run(session, self.run_id)
                proposal_id = await AuthorityStore(session).prepare_proposal(run_id=self.run_id,
                    operation=operation, arguments=prepared_args, title=f'Atlas proposes: {item.description}', capability_id=item.capability_id)
                proposal_evidence, _ = await self.record(session, operation, 'prepared',
                    {'status': 'approval_required', 'arguments': prepared_args, 'proposal_id': str(proposal_id)},
                    action_id=proposal_id, arguments=prepared_args, trust=item.trust)
                await session.commit()
                return proposal_id

        automatic_effect = (descriptor is not None and descriptor.effect != EffectKind.READ
            and descriptor.authority == AuthorityMode.AUTO and validation_error is None)
        if automatic_effect:
            async with self.factory() as session:
                await require_live_run(session, self.run_id)
                action_id = await AuthorityStore(session).begin_automatic_execution(run_id=self.run_id,
                    operation=operation, arguments=args, summary=descriptor.description, capability_id=descriptor.capability_id)
                await self.record(session, operation, 'executing', {'status': 'executing'},
                    action_id=action_id, arguments=args, trust='internal')
                await session.commit()
        result = await self.runtime.call(operation, args, proposal_sink=propose)
        if proposal_evidence is not None:
            evidence_id = proposal_evidence
            frozen = result.model_dump(mode='json')
        elif action_id is not None:
            evidence_id, frozen = await self.finish_action(action_id, result, args)
        else:
            async with self.factory() as session:
                store = AuthorityStore(session)
                await store._lock_run(self.run_id)
                action_id = await store.record_execution(run_id=self.run_id, operation=operation,
                    arguments=args, status=result.status, summary=descriptor.description if descriptor else operation)
                evidence_id, frozen = await self.record(session, operation, result.status,
                    result.model_dump(mode='json'), action_id=action_id, arguments=args,
                    trust=descriptor.trust if descriptor else 'external')
                # The canonical payload lives only in the observation/blob.
                action = await session.get(ActionRow, action_id)
                action.evidence = {**action.evidence, 'evidence_id': str(evidence_id)}
                await session.commit()
        payload = dict(frozen)
        payload['evidence_id'] = str(evidence_id)
        # Binary data is transient provider input. Its exact snapshot is already
        # durable, and only its artifact reference is written to PostgreSQL.
        output = result.output
        if isinstance(output, dict) and isinstance(output.get('resource'), dict):
            resource = output['resource']
            if resource.get('data_base64'):
                public_output = dict(payload.get('output') or {})
                public_output['resource'] = {**public_output.get('resource', {}), 'data_base64': resource['data_base64']}
                payload['output'] = public_output
        return payload
