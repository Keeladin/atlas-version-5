"""Shared deterministic dispatch and evidence handling for foreground/scheduled runs."""
from typing import Any
from uuid import UUID

from atlas.actions.authority import AuthorityStore
from atlas.actions.models import ActionStatus
from atlas.capabilities import AuthorityMode, CapabilityCallResult, EffectKind
from atlas.persistence.models import ActionRow, OwnerAttentionRow, TranscriptRow
from atlas.runtime.invocation import current_run_id, current_transcript_id
from atlas.runtime.observations import EvidenceStore
from atlas.runtime.recovery import require_live_run


def action_status_for_result(result) -> ActionStatus:
    phase = result.output.get('failure_phase') if isinstance(result.output, dict) else None
    if result.status == 'succeeded':
        return ActionStatus.SUCCEEDED
    if result.status in {'forbidden', 'unavailable'} or phase in {'before_dispatch', 'completed'}:
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
        self.output_artifacts: list[dict[str, Any]] = []
        self._output_artifact_ids: set[str] = set()

    def remember_output_artifact(self, result) -> None:
        if result.status != 'succeeded' or not isinstance(result.output, dict):
            return
        artifact = result.output.get('artifact')
        if not isinstance(artifact, dict):
            return
        artifact_id = str(artifact.get('artifact_id') or '')
        if not artifact_id or artifact_id in self._output_artifact_ids:
            return
        self._output_artifact_ids.add(artifact_id)
        self.output_artifacts.append({**artifact, 'operation': result.operation_id})

    async def _managed_task_grants(self) -> set[str]:
        async with self.factory() as session:
            row = await session.get(TranscriptRow, self.transcript_id)
            state = dict(row.active_task_state or {}) if row is not None else {}
        if state.get('mode') != 'managed' or state.get('status') != 'active':
            return set()
        return {
            str(item).strip()
            for item in (state.get('authority_grants') or [])
            if str(item).strip()
        }

    async def _prepare_managed_task_create(self, arguments: dict[str, Any]) -> dict[str, Any]:
        prepared = dict(arguments)
        raw_grants = prepared.get('authority_grants')
        grants = [str(item).strip() for item in raw_grants] if isinstance(raw_grants, list) else []
        prepared['authority_grants'] = await self.runtime.preflight_authority_grants(grants)
        prepared.setdefault('source_chat_id', str(self.transcript_id))
        return prepared

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
            cards = await self.runtime.search_cards_current(query, int(arguments.get('limit') or 8))
            grants = await self._managed_task_grants()
            for card in cards:
                if str(card.get('id') or '') in grants and card.get('authority') == AuthorityMode.APPROVAL_REQUIRED.value:
                    card['authority'] = AuthorityMode.AUTO.value
                    card['authority_source'] = 'managed_task_grant'
            result = {'operations': cards}
            async with self.factory() as session:
                evidence_id, _ = await EvidenceStore(session, self.artifacts).record(self.transcript_id,
                    operation=name, phase='searched', detail={'query': query, 'result': result},
                    run_id=self.run_id, checkpoint=False, trust='internal')
                await session.commit()
            return {**result, 'evidence_id': str(evidence_id)}
        if name != 'atlas_capability_call':
            return {'status': 'unavailable', 'message': 'Unknown Atlas capability control tool.'}
        operation = str(arguments.get('operation_id') or '')
        raw_args = arguments.get('arguments', {})
        args = dict(raw_args) if isinstance(raw_args, dict) else raw_args
        preflight_error: str | None = None
        if operation == 'workspace.tasks.create' and isinstance(args, dict):
            try:
                args = await self._prepare_managed_task_create(args)
            except ValueError as exc:
                preflight_error = str(exc)
        # Effect classification must remain stable across owner policy changes.
        # Dispatch checks current permission separately after action identity commits.
        descriptor = self.runtime.descriptor(operation)
        validation_error = preflight_error or self.runtime.validate_arguments(operation, args)
        action_id = None
        proposal_evidence = None
        task_grant_applied = False

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

        authority = None
        if descriptor is not None and validation_error is None:
            try:
                authority = await self.runtime.resolve_authority(operation, args)
                if operation in await self._managed_task_grants():
                    granted = await self.runtime.apply_task_grant(operation, authority)
                    task_grant_applied = (
                        authority == AuthorityMode.APPROVAL_REQUIRED
                        and granted == AuthorityMode.AUTO
                    )
                    authority = granted
            except Exception:  # noqa: BLE001 - runtime.call reports the failure as a tool result
                authority = None
        automatic_effect = (descriptor is not None and descriptor.effect != EffectKind.READ
            and authority == AuthorityMode.AUTO and validation_error is None)
        if automatic_effect:
            async with self.factory() as session:
                await require_live_run(session, self.run_id)
                action_id = await AuthorityStore(session).begin_automatic_execution(run_id=self.run_id,
                    operation=operation, arguments=args, summary=descriptor.description, capability_id=descriptor.capability_id)
                await self.record(session, operation, 'executing', {
                    'status': 'executing',
                    'authority_source': 'managed_task_grant' if task_grant_applied else 'atlas_control',
                }, action_id=action_id, arguments=args, trust='internal')
                await session.commit()
        if preflight_error is not None:
            result = CapabilityCallResult(
                status='failed',
                operation_id=operation,
                output={'failure_phase': 'before_dispatch'},
                message=preflight_error,
            )
        else:
            context_token = current_transcript_id.set(self.transcript_id)
            run_token = current_run_id.set(self.run_id)
            try:
                result = await self.runtime.call(operation, args, proposal_sink=propose, authority=authority)
            finally:
                current_run_id.reset(run_token)
                current_transcript_id.reset(context_token)
            if (
                operation == 'workspace.tasks.create'
                and result.status == 'succeeded'
                and isinstance(result.output, dict)
            ):
                result.output = {
                    **result.output,
                    'authority_preflight': {
                        'verified': True,
                        'operations': list(args.get('authority_grants') or []),
                    },
                }
        self.remember_output_artifact(result)
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