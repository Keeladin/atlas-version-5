from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select

from atlas.actions.authority import AuthorityStore
from atlas.actions.models import RunKind
from atlas.artifacts.store import ArtifactStore
from atlas.capabilities.service import CapabilityRuntime
from atlas.config import Settings
from atlas.db import get_session_factory
from atlas.persistence.models import RunRow, ScheduledTaskRow
from atlas.providers import OpenAIProvider
from atlas.runtime.conversation import build_model_instructions
from atlas.runtime.execution import RunExecutor
from atlas.runtime.observations import EvidenceStore
from atlas.runtime.recovery import interrupt_run, maintain_heartbeat, require_live_run
from atlas.transcript.models import Actor, TextBlock
from atlas.transcript.repository import TranscriptRepository

from .service import ScheduleService

logger = logging.getLogger(__name__)


async def claim_due(settings: Settings) -> int:
    """Advancement and the immutable queued occurrence commit together."""
    factory = get_session_factory()
    count = 0
    async with factory() as session:
        service = ScheduleService(session, settings.owner_timezone)
        for task in await service.due():
            outstanding = (await session.execute(select(RunRow.id).where(
                RunRow.schedule_id == task.id, RunRow.status.in_(['queued', 'running', 'waiting_for_owner', 'uncertain'])
            ).limit(1))).scalar_one_or_none()
            if outstanding is not None:
                continue
            snapshot = {'task_id': str(task.id), 'title': task.title, 'prompt': task.prompt,
                'timezone': task.timezone, 'scheduled_for': task.next_run_at.isoformat()}
            repo = TranscriptRepository(session)
            transcript = await repo.create(kind='scheduled')
            run_id = await AuthorityStore(session).create_run(transcript_id=transcript.id,
                intent=task.prompt, kind=RunKind.SCHEDULED)
            run = await session.get(RunRow, run_id)
            run.schedule_id = task.id
            run.scheduled_for = task.next_run_at
            run.trigger_snapshot = snapshot
            run.status = 'queued'
            run.inference_status = 'queued'
            run.inference_active = False
            run.heartbeat_at = None
            await repo.append_turn(transcript.id, Actor.SYSTEM,
                [TextBlock(text=f"Scheduled occurrence: {snapshot}")])
            service.advance_before_run(task)
            task.last_status = 'queued'
            count += 1
        await session.commit()
    return count


async def _execute_task(run_id: UUID, settings: Settings, runtime: CapabilityRuntime) -> None:
    """Claim a previously persisted occurrence; never reconstruct it from mutable intent."""
    if "atlas.schedules" not in await runtime.enabled_capabilities():
        return
    factory = get_session_factory()
    artifacts = ArtifactStore(settings.artifact_dir)
    async with factory() as session:
        run = (await session.execute(select(RunRow).where(RunRow.id == run_id)
            .with_for_update(skip_locked=True))).scalar_one_or_none()
        if run is None or run.inference_status != 'queued':
            return
        run.inference_active = True
        run.inference_status = 'running'
        run.status = 'running'
        run.heartbeat_at = datetime.now(UTC)
        transcript_id = run.transcript_id
        snapshot = dict(run.trigger_snapshot)
        await session.commit()
    completed = False
    try:
        async with maintain_heartbeat(factory, run_id):
            provider = OpenAIProvider(api_key=settings.openai_api_key or '', model=settings.openai_model,
                capability_call_limit=settings.capability_call_limit,
                capability_completion_reserve=settings.capability_completion_reserve, capability_policy=runtime.enabled_capabilities, input_token_budget=min(settings.working_context_tokens, settings.openai_context_window))
            executor = RunExecutor(factory, runtime, artifacts, run_id=run_id, transcript_id=transcript_id, checkpoint=False)
            async def observation_handler(payload):
                async with factory() as session:
                    evidence_id, _ = await EvidenceStore(session, artifacts).record(transcript_id, operation='provider.openai',
                        phase='observed', detail=payload, run_id=run_id, checkpoint=False)
                    await session.commit()
                    return str(evidence_id)
            messages = [{'role': 'user', 'content':
                f"Scheduled owner intent for {snapshot['scheduled_for']} ({snapshot['timezone']}):\n{snapshot['prompt']}"}]
            chunks = []
            async for delta in provider.stream_text(instructions=build_model_instructions(
                    await runtime.compact_index_current(), active_task_enabled=False,
                    owner_timezone=settings.owner_timezone,
                ),
                    messages=messages, tool_handler=executor.tool_handler, observation_handler=observation_handler):
                chunks.append(delta)
        answer = ''.join(chunks).strip()
        async with factory() as session:
            await require_live_run(session, run_id)
            if answer:
                await TranscriptRepository(session).append_turn(transcript_id, Actor.ATLAS, [TextBlock(text=answer)])
            await AuthorityStore(session).finish_run(run_id)
            run = await session.get(RunRow, run_id)
            task = await session.get(ScheduledTaskRow, run.schedule_id)
            if task is not None:
                task.last_status = run.status
                task.last_result = answer[:4000] or None
            await session.commit()
        completed = True
    finally:
        if not completed:
            await asyncio.shield(interrupt_run(factory, artifacts, run_id,
                reason='Scheduled inference was interrupted; the occurrence and all action evidence were retained.'))


async def run_due_once(settings: Settings, runtime: CapabilityRuntime) -> int:
    if settings.openai_api_key is None or "atlas.schedules" not in await runtime.enabled_capabilities():
        return 0
    await claim_due(settings)
    async with get_session_factory()() as session:
        queued = (await session.execute(select(RunRow.id).where(RunRow.kind == 'scheduled',
            RunRow.inference_status == 'queued').order_by(RunRow.scheduled_for).limit(10))).scalars().all()
    for run_id in queued:
        try:
            await _execute_task(run_id, settings, runtime)
        except Exception:
            logger.exception('Scheduled inference failed')
    return len(queued)


async def scheduler_loop(settings: Settings, runtime: CapabilityRuntime) -> None:
    while True:
        try:
            await run_due_once(settings, runtime)
        except Exception:
            logger.exception('Scheduled-task polling failed')
        await asyncio.sleep(max(5, settings.scheduler_poll_seconds))
