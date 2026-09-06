from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from atlas.actions.authority import AuthorityStore
from atlas.actions.models import ActionStatus, RunKind
from atlas.capabilities import AuthorityMode, EffectKind
from atlas.capabilities.service import CapabilityRuntime
from atlas.config import Settings
from atlas.db import get_session_factory
from atlas.persistence.models import ScheduledTaskRow
from atlas.providers import OpenAIProvider
from atlas.runtime.conversation import (
    build_model_instructions,
    turns_to_provider_messages,
)
from atlas.transcript.models import Actor, TextBlock, ToolObservationBlock
from atlas.transcript.repository import TranscriptRepository

from .service import ScheduleService

logger = logging.getLogger(__name__)


async def _execute_task(task_id: UUID, settings: Settings, runtime: CapabilityRuntime) -> None:
    factory = get_session_factory()
    async with factory() as session:
        task = await session.get(ScheduledTaskRow, task_id)
        if task is None:
            return
        title, prompt, timezone = task.title, task.prompt, task.timezone
        repository = TranscriptRepository(session)
        transcript = await repository.create(kind="scheduled")
        fired_at = datetime.now(UTC)
        await repository.append_turn(transcript.id, Actor.SYSTEM, [TextBlock(text=f"Scheduled task fired: {title}\n{prompt}")])
        run_id = await AuthorityStore(session).create_run(
            transcript_id=transcript.id, intent=f"Scheduled task: {title}", kind=RunKind.SCHEDULED,
        )
        await session.commit()
        turns = await repository.list_turns(transcript.id)

    provider = OpenAIProvider(api_key=settings.openai_api_key or "", model=settings.openai_model, capability_call_limit=settings.capability_call_limit, capability_completion_reserve=settings.capability_completion_reserve)
    messages = turns_to_provider_messages(turns)
    messages.append({"role": "user", "content": f"Atlas runtime scheduled task '{title}' fired at {fired_at.isoformat()} ({timezone}). Execute this task now:\n\n{prompt}"})
    async def tool_handler(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "atlas_capability_search":
            query = str(arguments.get("query") or "")
            limit = int(arguments.get("limit") or 8)
            result = {"operations": [item.model_dump(mode="json") for item in runtime.search(query, limit)]}
            async with factory() as observation_session:
                await TranscriptRepository(observation_session).append_turn(
                    transcript.id, Actor.TOOL,
                    [ToolObservationBlock(operation="atlas_capability_search", phase="searched", summary="Scheduled capability search", detail={"query": query, "result": result})],
                )
                await observation_session.commit()
            return result
        if name != "atlas_capability_call":
            return {"status": "unavailable", "message": "Unknown Atlas capability control tool."}
        operation_id = str(arguments.get("operation_id") or "")
        operation_arguments = arguments.get("arguments")
        if not isinstance(operation_arguments, dict):
            operation_arguments = {}

        async def proposal_sink(descriptor, proposed_arguments):
            async with factory() as proposal_session:
                store = AuthorityStore(proposal_session)
                proposal_id = await store.prepare_proposal(
                    run_id=run_id,
                    operation=descriptor.id,
                    arguments=proposed_arguments,
                    title=f"Atlas proposes: {descriptor.description}",
                    capability_id=descriptor.capability_id,
                )
                await proposal_session.commit()
                return proposal_id

        descriptor = runtime.descriptor(operation_id)
        automatic_effect = (
            descriptor is not None
            and descriptor.authority == AuthorityMode.AUTO
            and descriptor.effect != EffectKind.READ
        )
        action_id = None
        if automatic_effect and descriptor is not None:
            async with factory() as activity_session:
                action_id = await AuthorityStore(activity_session).begin_automatic_execution(
                    run_id=run_id, operation=operation_id, arguments=operation_arguments,
                    summary=descriptor.description, capability_id=descriptor.capability_id,
                )
                await activity_session.commit()

        result = await runtime.call(operation_id, operation_arguments, proposal_sink=proposal_sink)
        if result.status == "approval_required":
            async with factory() as observation_session:
                await TranscriptRepository(observation_session).append_turn(
                    transcript.id, Actor.TOOL,
                    [ToolObservationBlock(
                        action_id=UUID(result.proposal_id) if result.proposal_id else None,
                        operation=operation_id, phase="prepared", summary=operation_id,
                        detail={"arguments": operation_arguments, "proposal_id": result.proposal_id, "status": result.status},
                    )],
                )
                await observation_session.commit()
        elif automatic_effect and action_id is not None:
            failure_phase = result.output.get("failure_phase") if isinstance(result.output, dict) else None
            if result.status == "succeeded":
                final_status = ActionStatus.SUCCEEDED
            elif result.status in {"unavailable", "forbidden"} or failure_phase == "before_dispatch":
                final_status = ActionStatus.FAILED
            else:
                final_status = ActionStatus.UNCERTAIN
            async with factory() as activity_session:
                await AuthorityStore(activity_session).complete(
                    action_id, status=final_status, result=result.model_dump(mode="json"), resolve_run=False,
                )
                await activity_session.commit()
            async with factory() as observation_session:
                await TranscriptRepository(observation_session).append_turn(
                    transcript.id, Actor.TOOL,
                    [ToolObservationBlock(action_id=action_id, operation=operation_id, phase=final_status.value, summary=operation_id, detail=result.model_dump(mode="json"))],
                )
                await observation_session.commit()
        else:
            async with factory() as activity_session:
                action_id = await AuthorityStore(activity_session).record_execution(
                    run_id=run_id, operation=operation_id, arguments=operation_arguments,
                    status=result.status, summary=descriptor.description if descriptor else operation_id,
                    output=result.model_dump(mode="json"),
                )
                await activity_session.commit()
            async with factory() as observation_session:
                await TranscriptRepository(observation_session).append_turn(
                    transcript.id, Actor.TOOL,
                    [ToolObservationBlock(action_id=action_id, operation=operation_id, phase=result.status, summary=operation_id, detail=result.model_dump(mode="json"))],
                )
                await observation_session.commit()
        return result.model_dump(mode="json")

    chunks: list[str] = []
    status = "succeeded"
    try:
        async for delta in provider.stream_text(
            instructions=build_model_instructions(runtime.compact_index()),
            messages=messages,
            tool_handler=tool_handler,
        ):
            chunks.append(delta)
    except Exception as exc:  # noqa: BLE001 - background boundary must persist failures
        status = "failed"
        chunks = [f"Scheduled task failed: {type(exc).__name__}: {exc}"]

    answer = "".join(chunks).strip()
    async with factory() as session:
        task = await session.get(ScheduledTaskRow, task_id)
        repository = TranscriptRepository(session)
        if answer:
            await repository.append_turn(transcript.id, Actor.ATLAS, [TextBlock(text=answer)])
        await AuthorityStore(session).record_execution(
            run_id=run_id, operation="schedules.run", arguments={"task_id": str(task_id)},
            status=status, summary=f"Scheduled task: {title}",
        )
        await AuthorityStore(session).finish_run(run_id, succeeded=status == "succeeded")
        if task is not None:
            task.last_status = status
            task.last_result = answer[:4000] if answer else None
        await session.commit()


async def run_due_once(settings: Settings, runtime: CapabilityRuntime) -> int:
    if settings.openai_api_key is None:
        return 0
    factory = get_session_factory()
    async with factory() as session:
        service = ScheduleService(session, settings.owner_timezone)
        rows = await service.due()
        task_ids = [row.id for row in rows]
        for row in rows:
            service.advance_before_run(row)
        await session.commit()
    for task_id in task_ids:
        await _execute_task(task_id, settings, runtime)
    return len(task_ids)


async def scheduler_loop(settings: Settings, runtime: CapabilityRuntime) -> None:
    while True:
        try:
            await run_due_once(settings, runtime)
        except Exception:
            logger.exception("Scheduled-task polling failed")
        await asyncio.sleep(max(5, settings.scheduler_poll_seconds))
