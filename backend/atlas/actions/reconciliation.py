from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from atlas.actions.authority import AuthorityStore
from atlas.config import Settings
from atlas.db import get_session_factory

logger = logging.getLogger(__name__)


async def reconcile_once(settings: Settings) -> int:
    stale_before = datetime.now(UTC) - timedelta(seconds=max(60, settings.action_stale_after_seconds))
    factory = get_session_factory()
    async with factory() as session:
        count = await AuthorityStore(session).reconcile_stale_executions(stale_before=stale_before)
        await session.commit()
        return count


async def reconciliation_loop(settings: Settings) -> None:
    while True:
        try:
            await reconcile_once(settings)
        except Exception:
            logger.exception("Action reconciliation failed")
        await asyncio.sleep(max(30, settings.action_reconcile_seconds))
