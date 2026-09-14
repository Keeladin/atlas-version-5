"""Generic unit health monitor: owner-listed systemd units become awareness events.

Read-only. `systemctl show` needs no privilege for system units, and the monitor never
starts, stops or restarts anything; action is delegated to event schedules acting through
governed MCP servers under owner policy.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from atlas.config import Settings
from atlas.notifications import NotificationEvent, NotificationService, Severity
from atlas.persistence.models import HostMonitorStateRow

logger = logging.getLogger(__name__)

MONITOR_ID = "units"
SOURCE = "runtime.units"
PROPERTIES = ("Id", "ActiveState", "SubState", "Result", "NRestarts", "ExecMainStartTimestamp", "LoadState")
TIMEOUT_SECONDS = 15
HEALTHY = {"active", "activating", "reloading"}


@dataclass
class UnitStatus:
    unit: str
    load_state: str = "unknown"
    active_state: str = "unknown"
    sub_state: str = "unknown"
    result: str = ""
    restarts: int = 0
    started_at: str = ""
    observed: bool = False

    @property
    def healthy(self) -> bool:
        return self.observed and self.active_state in HEALTHY


@dataclass
class UnitsState:
    units: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> UnitsState:
        units = (data or {}).get("units")
        return cls(units=dict(units) if isinstance(units, dict) else {})


def show_argv(units: list[str]) -> list[str]:
    return ["systemctl", "show", "--no-pager", "-p", ",".join(PROPERTIES), "--", *units]


def parse_show_output(raw: str, units: list[str]) -> list[UnitStatus]:
    """systemctl show prints one KEY=VALUE block per unit, blank-line separated, in argument order."""
    blocks: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in raw.splitlines():
        if not line.strip():
            if current:
                blocks.append(current)
                current = {}
            continue
        key, _, value = line.partition("=")
        current[key.strip()] = value.strip()
    if current:
        blocks.append(current)
    by_id = {block.get("Id", ""): block for block in blocks}
    statuses: list[UnitStatus] = []
    for index, unit in enumerate(units):
        block = by_id.get(unit) or (blocks[index] if index < len(blocks) and not blocks[index].get("Id") else None)
        if block is None:
            statuses.append(UnitStatus(unit=unit))
            continue
        try:
            restarts = int(block.get("NRestarts", "0") or 0)
        except ValueError:
            restarts = 0
        statuses.append(UnitStatus(unit=unit, load_state=block.get("LoadState", "unknown"), active_state=block.get("ActiveState", "unknown"),
            sub_state=block.get("SubState", "unknown"), result=block.get("Result", ""), restarts=restarts,
            started_at=block.get("ExecMainStartTimestamp", ""), observed=True))
    return statuses


async def read_units(units: list[str]) -> list[UnitStatus]:
    process = await asyncio.create_subprocess_exec(*show_argv(units), stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, stdin=asyncio.subprocess.DEVNULL)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=TIMEOUT_SECONDS)
    except TimeoutError:
        process.kill()
        raise RuntimeError("systemctl show timed out") from None
    if process.returncode != 0:
        raise RuntimeError(f"systemctl show failed with status {process.returncode}: {stderr.decode(errors='replace')[:200]}")
    return parse_show_output(stdout.decode(errors="replace"), units)


def _thread(unit: str) -> str:
    return f"host.unit:{unit}"


def apply_statuses(state: UnitsState, statuses: list[UnitStatus], *, now: datetime | None = None,
        ) -> tuple[UnitsState, list[NotificationEvent]]:
    """Pure transition: emits on health changes and restart-count increases, never on steady state."""
    now = now or datetime.now(UTC)
    events: list[NotificationEvent] = []
    for status in statuses:
        previous = state.units.get(status.unit) or {}
        was_healthy = previous.get("healthy")
        detail = {"unit": status.unit, "active_state": status.active_state, "sub_state": status.sub_state,
            "result": status.result, "restarts": status.restarts, "started_at": status.started_at, "open_url": "/"}
        if not status.observed:
            if was_healthy is not False:
                events.append(NotificationEvent(source=SOURCE, kind="unit_unobservable", severity=Severity.WARNING.value,
                    title=f"{status.unit} could not be observed", body="systemctl show returned nothing for this unit; it may not exist on this host.",
                    detail=detail, thread_key=_thread(status.unit)))
        elif status.healthy:
            if was_healthy is False:
                events.append(NotificationEvent(source=SOURCE, kind="unit_restored", severity=Severity.RESOLVED.value,
                    title=f"{status.unit} is active again", body=f"{status.unit} is {status.active_state} ({status.sub_state}).",
                    detail=detail, thread_key=_thread(status.unit)))
            elif was_healthy is True and status.restarts > int(previous.get("restarts") or 0):
                events.append(NotificationEvent(source=SOURCE, kind="unit_restarted", severity=Severity.INFO.value,
                    title=f"{status.unit} restarted", body=f"Restart count is now {status.restarts}.",
                    detail=detail, thread_key=f"host.restart:{status.unit}"))
        elif was_healthy is not False:
            events.append(NotificationEvent(source=SOURCE, kind="unit_unhealthy", severity=Severity.WARNING.value,
                title=f"{status.unit} is {status.active_state}",
                body=f"{status.unit} is {status.active_state} ({status.sub_state}){f', result {status.result}' if status.result and status.result != 'success' else ''}. Atlas does not restart it by itself.",
                detail=detail, thread_key=_thread(status.unit)))
        state.units[status.unit] = {"healthy": status.healthy if status.observed else False, "observed": status.observed,
            "active_state": status.active_state, "sub_state": status.sub_state, "restarts": status.restarts,
            "started_at": status.started_at, "seen_at": now.astimezone(UTC).isoformat()}
    for stale in [unit for unit in list(state.units) if unit not in {status.unit for status in statuses}]:
        state.units.pop(stale, None)
    return state, events


async def units_monitor_once(settings: Settings, factory, *, reader=None, now: datetime | None = None,
        monitor_id: str = MONITOR_ID) -> dict[str, Any]:
    units = settings.host_watch_unit_list
    if not units:
        return {"units": 0, "events": 0, "emitted": 0}
    now = now or datetime.now(UTC)
    statuses = await (reader(units) if reader is not None else read_units(units))
    async with factory() as session:
        row = await session.get(HostMonitorStateRow, monitor_id, with_for_update=True)
        if row is None:
            row = HostMonitorStateRow(monitor_id=monitor_id, cursor=None, state={})
            session.add(row)
            await session.flush()
        state, events = apply_statuses(UnitsState.from_json(row.state), statuses, now=now)
        service = NotificationService(session, repeat_minutes=settings.push_repeat_minutes)
        emitted = 0
        for event in events:
            if await service.emit(event, now=now) is not None:
                emitted += 1
        row.state = state.to_json()
        await session.commit()
    return {"units": len(units), "events": len(events), "emitted": emitted,
        "unhealthy": [status.unit for status in statuses if not status.healthy]}


async def units_monitor_loop(settings: Settings) -> None:
    from atlas.db import get_session_factory

    while True:
        try:
            await units_monitor_once(settings, get_session_factory())
        except Exception:
            logger.exception("Unit health monitor pass failed")
        await asyncio.sleep(max(5, settings.host_monitor_poll_seconds))
