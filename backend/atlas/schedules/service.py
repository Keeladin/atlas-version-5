from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from croniter import croniter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from atlas.persistence.models import ScheduledTaskRow


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _project(row: ScheduledTaskRow) -> dict[str, object]:
    return {
        "id": str(row.id), "title": row.title, "prompt": row.prompt,
        "schedule_kind": row.schedule_kind, "schedule_value": row.schedule_value,
        "timezone": row.timezone, "enabled": row.enabled,
        "next_run_at": row.next_run_at, "last_run_at": row.last_run_at,
        "last_status": row.last_status, "last_result": row.last_result,
    }


def next_run(kind: str, value: str, timezone: str, *, now: datetime | None = None) -> datetime:
    current = now or _utc_now()
    tz = ZoneInfo(timezone)
    if kind == "once":
        candidate = datetime.fromisoformat(value)
        if candidate.tzinfo is None:
            candidate = candidate.replace(tzinfo=tz)
        return candidate.astimezone(UTC)
    if kind == "interval":
        minutes = max(1, int(value))
        return current + timedelta(minutes=minutes)
    if kind == "cron":
        local_now = current.astimezone(tz)
        return croniter(value, local_now).get_next(datetime).astimezone(UTC)
    raise ValueError("schedule_kind must be once, interval, or cron")


class ScheduleService:
    def __init__(self, session: AsyncSession, default_timezone: str) -> None:
        self.session = session
        self.default_timezone = default_timezone

    async def list(self, *, include_disabled: bool = True) -> list[dict[str, object]]:
        query = select(ScheduledTaskRow).order_by(ScheduledTaskRow.next_run_at)
        if not include_disabled:
            query = query.where(ScheduledTaskRow.enabled.is_(True))
        rows = (await self.session.execute(query)).scalars().all()
        return [_project(row) for row in rows]

    async def create(self, arguments: dict[str, object]) -> dict[str, object]:
        title = str(arguments.get("title") or "Scheduled task").strip()
        prompt = str(arguments.get("prompt") or "").strip()
        kind = str(arguments.get("schedule_kind") or "once").strip().lower()
        value = str(arguments.get("schedule_value") or "").strip()
        timezone = str(arguments.get("timezone") or self.default_timezone).strip()
        if not prompt or not value:
            raise ValueError("prompt and schedule_value are required")
        due = next_run(kind, value, timezone)
        if kind == "once" and due <= _utc_now():
            raise ValueError("One-time schedules must be in the future")
        row = ScheduledTaskRow(
            title=title, prompt=prompt, schedule_kind=kind, schedule_value=value,
            timezone=timezone, enabled=True, next_run_at=due,
        )
        self.session.add(row)
        await self.session.flush()
        return _project(row)

    async def update(self, arguments: dict[str, object]) -> dict[str, object]:
        row = await self._get(str(arguments.get("task_id") or ""))
        for key in ("title", "prompt", "schedule_kind", "schedule_value", "timezone"):
            if key in arguments and arguments[key] is not None:
                setattr(row, key, str(arguments[key]).strip())
        if "enabled" in arguments:
            row.enabled = bool(arguments["enabled"])
        row.next_run_at = next_run(row.schedule_kind, row.schedule_value, row.timezone)
        await self.session.flush()
        return _project(row)

    async def delete(self, arguments: dict[str, object]) -> dict[str, object]:
        row = await self._get(str(arguments.get("task_id") or ""))
        task_id = str(row.id)
        await self.session.delete(row)
        await self.session.flush()
        return {"id": task_id, "deleted": True}

    async def due(self, limit: int = 10) -> list[ScheduledTaskRow]:
        rows = (await self.session.execute(
            select(ScheduledTaskRow)
            .where(ScheduledTaskRow.enabled.is_(True), ScheduledTaskRow.next_run_at <= _utc_now())
            .order_by(ScheduledTaskRow.next_run_at)
            .with_for_update(skip_locked=True)
            .limit(max(1, min(limit, 50)))
        )).scalars().all()
        return list(rows)

    def advance_before_run(self, row: ScheduledTaskRow, *, now: datetime | None = None) -> None:
        current = now or _utc_now()
        row.last_run_at = current
        row.last_status = "running"
        row.last_result = None
        if row.schedule_kind == "once":
            row.enabled = False
        else:
            row.next_run_at = next_run(row.schedule_kind, row.schedule_value, row.timezone, now=current)

    async def _get(self, task_id: str) -> ScheduledTaskRow:
        from uuid import UUID

        try:
            parsed = UUID(task_id)
        except ValueError as exc:
            raise ValueError("Invalid scheduled task ID") from exc
        row = await self.session.get(ScheduledTaskRow, parsed)
        if row is None:
            raise ValueError("Scheduled task not found")
        return row
