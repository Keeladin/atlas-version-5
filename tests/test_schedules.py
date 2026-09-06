from datetime import UTC, datetime

from atlas.schedules.service import next_run


def test_once_schedule_uses_owner_timezone_when_naive() -> None:
    due = next_run("once", "2026-09-07T07:00:00", "Africa/Johannesburg")
    assert due == datetime(2026, 9, 7, 5, 0, tzinfo=UTC)


def test_interval_schedule_advances_from_now() -> None:
    now = datetime(2026, 9, 6, 14, 0, tzinfo=UTC)
    assert next_run("interval", "90", "Africa/Johannesburg", now=now) == datetime(2026, 9, 6, 15, 30, tzinfo=UTC)


def test_cron_schedule_respects_timezone() -> None:
    now = datetime(2026, 9, 6, 14, 0, tzinfo=UTC)
    due = next_run("cron", "0 7 * * *", "Africa/Johannesburg", now=now)
    assert due == datetime(2026, 9, 7, 5, 0, tzinfo=UTC)
