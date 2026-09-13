"""Generic unit health monitor: systemctl show parsing and health transitions."""
from datetime import UTC, datetime

import pytest
from atlas.config import Settings
from atlas.monitors.units import (
    UnitsState,
    UnitStatus,
    apply_statuses,
    parse_show_output,
    show_argv,
    units_monitor_once,
)
from atlas.persistence.models import HostMonitorStateRow, NotificationRow
from sqlalchemy import select

NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
SHOW = """Id=desktop-commander.service
ActiveState=active
SubState=running
Result=success
NRestarts=0
ExecMainStartTimestamp=Sun 2026-09-13 08:44:35 SAST
LoadState=loaded

Id=empire-control.service
ActiveState=failed
SubState=failed
Result=exit-code
NRestarts=3
ExecMainStartTimestamp=
LoadState=loaded

Id=missing.service
ActiveState=inactive
SubState=dead
Result=success
NRestarts=0
ExecMainStartTimestamp=
LoadState=not-found
"""


def test_show_argv_is_fixed_and_units_follow_the_separator() -> None:
    argv = show_argv(["a.service", "--evil"])
    assert argv[:5] == ["systemctl", "show", "--no-pager", "-p", "Id,ActiveState,SubState,Result,NRestarts,ExecMainStartTimestamp,LoadState"]
    assert argv[5] == "--" and argv[6:] == ["a.service", "--evil"]


def test_parse_show_output_maps_blocks_to_units() -> None:
    statuses = parse_show_output(SHOW, ["desktop-commander.service", "empire-control.service", "missing.service", "ghost.service"])
    by_unit = {status.unit: status for status in statuses}
    assert by_unit["desktop-commander.service"].healthy and by_unit["desktop-commander.service"].restarts == 0
    assert not by_unit["empire-control.service"].healthy and by_unit["empire-control.service"].result == "exit-code"
    assert by_unit["missing.service"].load_state == "not-found" and not by_unit["missing.service"].healthy
    assert by_unit["ghost.service"].observed is False


def test_transitions_emit_only_on_change() -> None:
    state = UnitsState()
    healthy = UnitStatus(unit="a.service", active_state="active", sub_state="running", observed=True)
    failed = UnitStatus(unit="a.service", active_state="failed", sub_state="failed", result="exit-code", restarts=2, observed=True)
    state, events = apply_statuses(state, [healthy], now=NOW)
    assert events == []  # first sight of a healthy unit is not news
    state, events = apply_statuses(state, [healthy], now=NOW)
    assert events == []
    state, events = apply_statuses(state, [failed], now=NOW)
    assert [event.kind for event in events] == ["unit_unhealthy"]
    assert events[0].severity == "warning" and events[0].thread_key == "host.unit:a.service"
    assert "does not restart it by itself" in events[0].body and events[0].detail["result"] == "exit-code"
    state, events = apply_statuses(state, [failed], now=NOW)
    assert events == []
    restored = UnitStatus(unit="a.service", active_state="active", sub_state="running", restarts=3, observed=True)
    state, events = apply_statuses(state, [restored], now=NOW)
    assert [event.kind for event in events] == ["unit_restored"] and events[0].severity == "resolved"
    bounced = UnitStatus(unit="a.service", active_state="active", sub_state="running", restarts=4, observed=True)
    state, events = apply_statuses(state, [bounced], now=NOW)
    assert [event.kind for event in events] == ["unit_restarted"] and events[0].severity == "info"
    state, events = apply_statuses(state, [UnitStatus(unit="a.service")], now=NOW)
    assert [event.kind for event in events] == ["unit_unobservable"]
    state, events = apply_statuses(state, [UnitStatus(unit="a.service")], now=NOW)
    assert events == []
    state, events = apply_statuses(state, [], now=NOW)
    assert state.units == {}


def test_first_sight_of_an_unhealthy_unit_is_announced() -> None:
    failed = UnitStatus(unit="b.service", active_state="failed", sub_state="failed", observed=True)
    _, events = apply_statuses(UnitsState(), [failed], now=NOW)
    assert [event.kind for event in events] == ["unit_unhealthy"]


@pytest.mark.asyncio
async def test_monitor_pass_persists_state_and_emits_through_the_ledger(pg_factory) -> None:
    settings = Settings(host_watch_units="desktop-commander.service, empire-control.service")
    assert settings.host_watch_unit_list == ["desktop-commander.service", "empire-control.service"]

    async def reader(units):
        return parse_show_output(SHOW, units)

    result = await units_monitor_once(settings, pg_factory, reader=reader, now=NOW)
    assert result["emitted"] == 1 and result["unhealthy"] == ["empire-control.service"]
    result = await units_monitor_once(settings, pg_factory, reader=reader, now=NOW)
    assert result["emitted"] == 0
    async with pg_factory() as session:
        rows = (await session.execute(select(NotificationRow))).scalars().all()
        assert len(rows) == 1 and rows[0].source == "runtime.units" and rows[0].thread_key == "host.unit:empire-control.service"
        state = await session.get(HostMonitorStateRow, "units")
        assert state.state["units"]["empire-control.service"]["healthy"] is False
    assert await units_monitor_once(Settings(host_watch_units=""), pg_factory, reader=reader) == {"units": 0, "events": 0, "emitted": 0}
