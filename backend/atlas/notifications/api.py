from datetime import datetime
from typing import Annotated, Any
from urllib.parse import urlparse
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from atlas.api.deps import get_session
from atlas.config import get_settings
from atlas.db import get_session_factory
from atlas.persistence.models import NotificationRow, PushSubscriptionRow

from .dispatcher import deliver_pending, load_push_client
from .models import NotificationEvent, Severity
from .push import VapidKeys
from .service import NotificationService, _project

router = APIRouter(prefix="/api/notifications", tags=["notifications"])
push_router = APIRouter(prefix="/api/push", tags=["push"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]


def _service(session: AsyncSession) -> NotificationService:
    return NotificationService(session, repeat_minutes=get_settings().push_repeat_minutes)


@router.get("")
async def list_notifications(session: SessionDep, limit: int = Query(30, ge=1, le=100),
        before: datetime | None = None, include_superseded: bool = False) -> dict[str, Any]:
    service = _service(session)
    items = await service.list(limit=limit, before=before, include_superseded=include_superseded)
    return {"items": items, "unread": await service.unread_count()}


@router.post("/read-all")
async def read_all(session: SessionDep) -> dict[str, int]:
    count = await _service(session).mark_all_read()
    await session.commit()
    return {"updated": count}


@router.post("/{notification_id}/read")
async def mark_read(notification_id: UUID, session: SessionDep) -> dict[str, Any]:
    try:
        item = await _service(session).mark_read(notification_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await session.commit()
    return item


@router.post("/{notification_id}/resolve")
async def resolve(notification_id: UUID, session: SessionDep) -> dict[str, Any]:
    try:
        item = await _service(session).resolve(notification_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await session.commit()
    return item


class SubscriptionKeys(BaseModel):
    p256dh: str = Field(min_length=1, max_length=512)
    auth: str = Field(min_length=1, max_length=512)


class SubscribeBody(BaseModel):
    endpoint: str = Field(min_length=12, max_length=4096)
    keys: SubscriptionKeys
    user_agent: str = Field(default="", max_length=512)


class UnsubscribeBody(BaseModel):
    endpoint: str = Field(min_length=12, max_length=4096)


def _subscription_projection(row: PushSubscriptionRow) -> dict[str, Any]:
    return {
        "id": str(row.id), "host": urlparse(row.endpoint).netloc, "user_agent": row.user_agent or "",
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "last_success_at": row.last_success_at.isoformat() if row.last_success_at else None,
        "failure_count": row.failure_count or 0, "disabled": row.disabled_at is not None,
    }


@push_router.get("/vapid-public-key")
async def vapid_public_key() -> dict[str, Any]:
    settings = get_settings()
    public_key = None
    if settings.push_configured and settings.push_vapid_private_key_file is not None:
        try:
            public_key = VapidKeys.load(settings.push_vapid_private_key_file).public_key_b64url()
        except (OSError, ValueError):
            public_key = None
    return {"configured": public_key is not None, "public_key": public_key, "subject": settings.push_vapid_subject}


@push_router.get("/subscriptions")
async def list_subscriptions(session: SessionDep) -> dict[str, Any]:
    rows = (await session.execute(select(PushSubscriptionRow).order_by(PushSubscriptionRow.created_at))).scalars().all()
    return {"items": [_subscription_projection(row) for row in rows]}


@push_router.post("/subscribe")
async def subscribe(body: SubscribeBody, session: SessionDep) -> dict[str, Any]:
    parsed = urlparse(body.endpoint)
    if parsed.scheme != "https" or not parsed.netloc:
        raise HTTPException(status_code=422, detail="Push endpoint must be an https URL")
    row = (await session.execute(select(PushSubscriptionRow).where(PushSubscriptionRow.endpoint == body.endpoint)
        .with_for_update())).scalar_one_or_none()
    if row is None:
        row = PushSubscriptionRow(endpoint=body.endpoint)
        session.add(row)
    row.p256dh, row.auth, row.user_agent = body.keys.p256dh, body.keys.auth, body.user_agent[:512]
    row.disabled_at, row.failure_count = None, 0
    await session.flush()
    await session.commit()
    return _subscription_projection(row)


@push_router.post("/unsubscribe")
async def unsubscribe(body: UnsubscribeBody, session: SessionDep) -> dict[str, bool]:
    row = (await session.execute(select(PushSubscriptionRow)
        .where(PushSubscriptionRow.endpoint == body.endpoint))).scalar_one_or_none()
    if row is not None:
        await session.delete(row)
        await session.commit()
    return {"removed": row is not None}


@push_router.delete("/subscriptions/{subscription_id}")
async def delete_subscription(subscription_id: UUID, session: SessionDep) -> dict[str, bool]:
    row = await session.get(PushSubscriptionRow, subscription_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Push subscription not found")
    await session.delete(row)
    await session.commit()
    return {"removed": True}


@push_router.post("/test")
async def send_test(session: SessionDep) -> dict[str, Any]:
    settings = get_settings()
    item = await _service(session).emit(NotificationEvent(source="runtime.push", kind="test",
        severity=Severity.WARNING.value, title="Atlas test notification",
        body="Push notifications reach this device.", thread_key="push.test", detail={"open_url": "/control"}))
    await session.commit()
    await deliver_pending(get_session_factory(), load_push_client(settings))
    row = await session.get(NotificationRow, UUID(item["id"]), populate_existing=True) if item else None
    return {"notification": _project(row) if row else item, "push_status": row.push_status if row else None,
        "results": dict(row.push_result or {}) if row else {}, "configured": settings.push_configured}
