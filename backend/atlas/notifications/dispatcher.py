"""Outbox drain: pending notifications -> every enabled device. Crash-safe and process-agnostic."""
import asyncio
import hashlib
import logging
from datetime import UTC, datetime

from sqlalchemy import select

from atlas.config import Settings
from atlas.db import get_session_factory
from atlas.persistence.models import NotificationRow, PushSubscriptionRow

from .push import PushClient, VapidKeys, build_payload

logger = logging.getLogger(__name__)
GONE = {404, 410}


def _endpoint_hash(endpoint: str) -> str:
    return hashlib.sha256(endpoint.encode()).hexdigest()[:12]


def load_push_client(settings: Settings) -> PushClient | None:
    if not settings.push_configured or settings.push_vapid_private_key_file is None:
        return None
    try:
        return PushClient(VapidKeys.load(settings.push_vapid_private_key_file), settings.push_vapid_subject)
    except (OSError, ValueError) as exc:
        logger.warning("Push disabled: VAPID key unusable (%s)", type(exc).__name__)
        return None


async def deliver_pending(factory, client: PushClient | None, *, limit: int = 20) -> int:
    now = datetime.now(UTC)
    async with factory() as session:
        rows = list((await session.execute(select(NotificationRow).where(NotificationRow.push_status == "pending")
            .order_by(NotificationRow.created_at).limit(limit).with_for_update(skip_locked=True))).scalars().all())
        if not rows:
            return 0
        subscriptions = list((await session.execute(select(PushSubscriptionRow)
            .where(PushSubscriptionRow.disabled_at.is_(None)))).scalars().all())
        for row in rows:
            row.push_attempted_at = now
            if client is None or not subscriptions:
                row.push_status = "skipped"
                continue
            payload = build_payload(row)
            results: dict[str, dict] = {}
            delivered = False
            for subscription in subscriptions:
                info = {"endpoint": subscription.endpoint,
                    "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth}}
                try:
                    status = await asyncio.to_thread(client.send, info, payload)
                except Exception as exc:  # noqa: BLE001 - one device failing must not block the rest
                    logger.warning("Push send raised %s", type(exc).__name__)
                    status = 0
                results[_endpoint_hash(subscription.endpoint)] = {"status": status, "at": now.isoformat()}
                if 200 <= status < 300:
                    delivered = True
                    subscription.last_success_at, subscription.failure_count = now, 0
                else:
                    subscription.failure_count = (subscription.failure_count or 0) + 1
                    if status in GONE:
                        subscription.disabled_at = now
            row.push_result = results
            row.push_status = "sent" if delivered else "failed"
        await session.commit()
        return len(rows)


async def push_delivery_loop(settings: Settings) -> None:
    client = load_push_client(settings)
    if client is None:
        logger.info("Push delivery running without VAPID configuration; pending notifications will be skipped")
    while True:
        try:
            await deliver_pending(get_session_factory(), client)
        except Exception:
            logger.exception("Push delivery failed")
        await asyncio.sleep(max(1, settings.push_delivery_poll_seconds))
