"""Inference liveness is separate from task meaning and external effect truth."""
import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select, update

from atlas.actions.authority import AuthorityStore
from atlas.persistence.models import ActionRow, OwnerAttentionRow, RunRow
from atlas.runtime.observations import EvidenceStore


class RunInterrupted(RuntimeError):
    pass


async def require_live_run(session, run_id):
    run = await AuthorityStore(session)._lock_run(run_id)
    if run is None or not run.inference_active:
        raise RunInterrupted('This inference run no longer owns execution; continue in a new owner turn')
    return run


@asynccontextmanager
async def maintain_heartbeat(factory, run_id):
    owner_task = asyncio.current_task()
    async def heartbeat():
        while True:
            async with factory() as session:
                changed = await session.execute(update(RunRow)
                    .where(RunRow.id == run_id, RunRow.inference_active.is_(True))
                    .values(heartbeat_at=datetime.now(UTC)))
                await session.commit()
                if changed.rowcount != 1:
                    owner_task.cancel()
                    return
            await asyncio.sleep(10)
    task = asyncio.create_task(heartbeat())
    def heartbeat_done(done):
        if not done.cancelled() and done.exception() is not None:
            owner_task.cancel()
    task.add_done_callback(heartbeat_done)
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def interrupt_run(factory, artifacts, run_id, *, reason: str) -> None:
    async with factory() as session:
        store = AuthorityStore(session)
        run = await store._lock_run(run_id)
        if run is None or run.inference_status not in {'running', 'queued'}:
            return
        run.inference_active = False
        run.inference_status = 'interrupted'
        await session.flush()
        # Any effect already dispatched stays in the action ledger and becomes
        # uncertain. Never replay it because an inference run was interrupted.
        actions = (await session.execute(select(ActionRow).where(
            ActionRow.run_id == run_id, ActionRow.status == 'executing'))).scalars().all()
        for action in actions:
            changed = await session.execute(update(ActionRow).where(ActionRow.id == action.id,
                ActionRow.status == 'executing').values(status='uncertain', updated_at=datetime.now(UTC)))
            if changed.rowcount == 1:
                await store._mark_uncertain(action, reason=reason)
                if run.transcript_id is not None:
                    await EvidenceStore(session, artifacts).record(run.transcript_id,
                        operation=action.operation, phase='uncertain', detail={'status': 'uncertain', 'reason': reason},
                        run_id=run_id, action_id=action.id, checkpoint=run.kind == 'foreground', trust='internal')
        await store._recompute_run(run_id)
        if run.transcript_id is not None:
            await EvidenceStore(session, artifacts).record(run.transcript_id,
                operation='runtime.inference', phase='interrupted', detail={'run_id': str(run_id), 'reason': reason},
                run_id=run_id, checkpoint=False, trust='internal')
        existing = (await session.execute(select(OwnerAttentionRow.id).where(
            OwnerAttentionRow.run_id == run_id, OwnerAttentionRow.action_id.is_(None),
            OwnerAttentionRow.state == 'interrupted', OwnerAttentionRow.resolved.is_(False)))).scalar_one_or_none()
        if existing is None:
            session.add(OwnerAttentionRow(run_id=run_id, state='interrupted',
                title='Atlas work was interrupted', detail={'message': reason,
                    'resume': 'Continue with a new owner message; exact prior evidence and task state are retained.'}))
        await session.commit()


async def recover_abandoned_runs(factory, artifacts, *, stale_seconds: int = 300) -> int:
    cutoff = datetime.now(UTC) - timedelta(seconds=max(60, stale_seconds))
    async with factory() as session:
        ids = (await session.execute(select(RunRow.id).where(RunRow.inference_status == 'running',
            or_(RunRow.heartbeat_at < cutoff, RunRow.heartbeat_at.is_(None))))).scalars().all()
    recovered = 0
    for run_id in ids:
        # Atomically fence the producer, rechecking liveness under its row lock.
        async with factory() as session:
            run = await AuthorityStore(session)._lock_run(run_id)
            if run is None or run.inference_status != 'running' or (run.heartbeat_at and run.heartbeat_at >= cutoff):
                continue
            run.inference_active = False
            await session.commit()
        await interrupt_run(factory, artifacts, run_id, reason='Inference stopped before a durable completion. Task state and effect evidence were retained.')
        recovered += 1
    return recovered
