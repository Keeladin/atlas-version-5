from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from atlas.actions.authority import AuthorityStore
from atlas.artifacts.store import ArtifactStore
from atlas.config import Settings
from atlas.db import get_session_factory
from atlas.runtime.observations import EvidenceStore
from atlas.runtime.recovery import recover_abandoned_runs

logger = logging.getLogger(__name__)


async def reconcile_once(settings: Settings) -> int:
    stale_before = datetime.now(UTC) - timedelta(seconds=max(60, settings.action_stale_after_seconds))
    factory = get_session_factory()
    async with factory() as session:
        async def observed(action, run):
            if run is not None and run.transcript_id is not None:
                await EvidenceStore(session, ArtifactStore(settings.artifact_dir)).record(run.transcript_id,
                    operation=action.operation, phase="uncertain", detail={"status": "uncertain",
                        "reason": "Execution stopped before its external outcome was confirmed."},
                    run_id=run.id, action_id=action.id, checkpoint=run.kind == "foreground", trust="internal")
        count = await AuthorityStore(session).reconcile_stale_executions(stale_before=stale_before, observation_sink=observed)
        await session.commit()
    count += await recover_abandoned_runs(factory, ArtifactStore(settings.artifact_dir),
        stale_seconds=settings.action_stale_after_seconds)
    return count


async def reconciliation_loop(settings: Settings) -> None:
    while True:
        try:
            await reconcile_once(settings)
        except Exception:
            logger.exception("Action reconciliation failed")
        await asyncio.sleep(max(30, settings.action_reconcile_seconds))
