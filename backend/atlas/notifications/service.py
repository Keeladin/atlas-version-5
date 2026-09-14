"""Transactional emit with thread supersession; delivery is an outbox drained separately.

The core is synchronous so it can run both from the async service (via run_sync) and from the
ORM flush hook that mirrors owner-attention rows into this ledger.
"""
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from atlas.persistence.models import NotificationRow

from .models import REDACTED, NotificationEvent, Severity
from .policy import decide_push

OPEN, SUPERSEDED, RESOLVED = "open", "superseded", "resolved"


def _project(row: NotificationRow, *, audience: str = "owner") -> dict[str, Any]:
    detail = dict(row.detail or {})
    body = row.body or ""
    sensitive = [str(key) for key in (row.sensitive_fields or [])]
    if audience == "model":
        for key in sensitive:
            if key == "body":
                body = REDACTED
            elif key in detail:
                detail[key] = REDACTED
    return {
        "id": str(row.id), "source": row.source, "kind": row.kind, "severity": row.severity,
        "title": row.title, "body": body, "detail": detail, "sensitive_fields": sensitive,
        "thread_key": row.thread_key, "status": row.status, "read": row.read_at is not None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "resolved_at": row.resolved_at.isoformat() if row.resolved_at else None,
        "run_id": str(row.run_id) if row.run_id else None, "push_status": row.push_status,
    }


def _open_thread(session: Session, thread_key: str) -> list[NotificationRow]:
    return list(session.execute(
        select(NotificationRow).where(NotificationRow.thread_key == thread_key, NotificationRow.status == OPEN)
        .order_by(NotificationRow.created_at).with_for_update()
    ).scalars().all())


def _thread_push_history(session: Session, thread_key: str) -> tuple[bool, datetime | None]:
    pushed = session.execute(select(func.count()).select_from(NotificationRow).where(
        NotificationRow.thread_key == thread_key, NotificationRow.push_status.in_(["pending", "sent"])
    )).scalar_one()
    last_sent = session.execute(select(func.max(NotificationRow.push_attempted_at)).where(
        NotificationRow.thread_key == thread_key, NotificationRow.push_status == "sent"
    )).scalar_one()
    return bool(pushed), last_sent


def emit_sync(session: Session, event: NotificationEvent, *, now: datetime | None = None,
        repeat_minutes: int = 60) -> NotificationRow | None:
    """Write one event inside the caller's transaction without flushing.

    Returns the new row, the identical open row it deduplicated against, or None when a
    resolution had nothing to resolve.
    """
    now = now or datetime.now(UTC)
    open_rows: list[NotificationRow] = []
    pushed_before, last_push_at = False, None
    with session.no_autoflush:
        if event.thread_key:
            open_rows = _open_thread(session, event.thread_key)
            pushed_before, last_push_at = _thread_push_history(session, event.thread_key)
        if event.severity == Severity.RESOLVED.value:
            if not open_rows:
                return None
            for row in open_rows:
                row.status, row.resolved_at = RESOLVED, now
            status = RESOLVED
        else:
            for row in open_rows:
                if (row.kind, row.title, row.body or "", dict(row.detail or {})) == (
                        event.kind, event.title, event.body, dict(event.detail)):
                    return row
            for row in open_rows:
                row.status = SUPERSEDED
            status = OPEN
        decision = decide_push(event.severity, thread_pushed_before=pushed_before, last_push_at=last_push_at,
            now=now, repeat_minutes=repeat_minutes)
        row = NotificationRow(
            id=uuid4(), source=event.source, kind=event.kind, severity=event.severity, title=event.title,
            body=event.body, detail=dict(event.detail), sensitive_fields=list(event.sensitive_fields),
            thread_key=event.thread_key, status=status, resolved_at=now if status == RESOLVED else None,
            run_id=event.run_id, push_status=decision.status, push_quiet=decision.quiet, created_at=now,
            push_result={},
        )
        session.add(row)
    return row


def resolve_thread_sync(session: Session, thread_key: str, *, now: datetime | None = None) -> int:
    """Close a thread without a new event: the owner acted, nothing to announce."""
    with session.no_autoflush:
        rows = _open_thread(session, thread_key)
        for row in rows:
            row.status, row.resolved_at = RESOLVED, now or datetime.now(UTC)
    return len(rows)


project_notification = _project


class NotificationService:
    def __init__(self, session: AsyncSession, *, repeat_minutes: int = 60) -> None:
        self.session = session
        self.repeat_minutes = repeat_minutes

    async def _run(self, fn: Callable[[Session], Any]) -> Any:
        return await self.session.run_sync(fn)

    async def emit(self, event: NotificationEvent, *, now: datetime | None = None) -> dict[str, Any] | None:
        row = await self._run(lambda s: emit_sync(s, event, now=now, repeat_minutes=self.repeat_minutes))
        if row is None:
            return None
        await self.session.flush()
        return _project(row)

    async def list(self, *, limit: int = 30, before: datetime | None = None, include_superseded: bool = False,
            audience: str = "owner") -> list[dict[str, Any]]:
        query = select(NotificationRow)
        if not include_superseded:
            query = query.where(NotificationRow.status != SUPERSEDED)
        if before is not None:
            query = query.where(NotificationRow.created_at < before)
        rows = (await self.session.execute(query.order_by(NotificationRow.created_at.desc())
            .limit(max(1, min(limit, 100))))).scalars().all()
        return [_project(row, audience=audience) for row in rows]

    async def unread_count(self) -> int:
        return (await self.session.execute(select(func.count()).select_from(NotificationRow).where(
            NotificationRow.read_at.is_(None), NotificationRow.status != SUPERSEDED))).scalar_one()

    async def mark_read(self, notification_id: UUID) -> dict[str, Any]:
        row = await self.session.get(NotificationRow, notification_id)
        if row is None:
            raise LookupError("Notification not found")
        if row.read_at is None:
            row.read_at = datetime.now(UTC)
        await self.session.flush()
        return _project(row)

    async def mark_all_read(self) -> int:
        result = await self.session.execute(update(NotificationRow).where(NotificationRow.read_at.is_(None))
            .values(read_at=datetime.now(UTC)))
        return result.rowcount or 0

    async def resolve(self, notification_id: UUID) -> dict[str, Any]:
        row = await self.session.get(NotificationRow, notification_id)
        if row is None:
            raise LookupError("Notification not found")
        now = datetime.now(UTC)
        if row.thread_key:
            await self.resolve_thread(row.thread_key)
        if row.status == OPEN:
            row.status, row.resolved_at = RESOLVED, now
        if row.read_at is None:
            row.read_at = now
        await self.session.flush()
        return _project(row)

    async def resolve_thread(self, thread_key: str) -> int:
        return await self._run(lambda s: resolve_thread_sync(s, thread_key))

    async def model_emit_allowed(self, per_hour: int) -> bool:
        since = datetime.now(UTC) - timedelta(hours=1)
        count = (await self.session.execute(select(func.count()).select_from(NotificationRow).where(
            NotificationRow.source == "model", NotificationRow.created_at >= since))).scalar_one()
        return count < max(0, per_hour)
