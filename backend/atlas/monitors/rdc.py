"""Desktop Commander remote connector monitor.

Read-only by design. Every restart of the connector invalidates its persisted refresh token
("Invalid Refresh Token: Already Used") and forces a new device-authorization code, so the
monitor never restarts anything. It tails the connector's user journal, matches only the
known lifecycle lines, and tells the owner when a new device code is needed, when the device
is back online, and when the process is genuinely missing.

Only MESSAGE (truncated), __CURSOR and __REALTIME_TIMESTAMP are ever read from a journal entry;
the journal also carries large tool outputs that must never be persisted.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from atlas.config import Settings
from atlas.notifications import NotificationEvent, NotificationService, Severity
from atlas.persistence.models import HostMonitorStateRow

logger = logging.getLogger(__name__)

MONITOR_ID = "rdc"
SOURCE = "runtime.rdc"
MESSAGE_LIMIT = 512
JOURNAL_TIMEOUT_SECONDS = 20
JOURNAL_OUTPUT_LIMIT = 32 * 1024 * 1024

_UUID = r"([0-9a-fA-F-]{36})"
RE_STARTING = re.compile(r"Starting MCP Device")
RE_PERSISTED = re.compile(rf"Found persisted session for device {_UUID}")
RE_SESSION_INVALID = re.compile(r"Persisted session invalid")
RE_SESSION_RESTORED = re.compile(r"Session restored")
RE_AUTH_FLOW = re.compile(r"Starting device authorization flow")
RE_CODE_RECEIVED = re.compile(r"Device code received")
RE_VERIFY_URL = re.compile(r"^(https://\S+/device/verify\S*)$")
RE_CODE = re.compile(r"^([A-Z0-9]{4}-[A-Z0-9]{4})$")
RE_EXPIRES = re.compile(r"Code expires in (\d+) minutes")
RE_AUTHENTICATED = re.compile(rf"Device ID authenticated: {_UUID}")
RE_ONLINE = re.compile(r"Device marked as online")
RE_READY = re.compile(r"Device ready:")
RE_DEVICE_NAME = re.compile(r"Device Name:\s+(\S+)")
RE_SHUTDOWN = re.compile(r"Shutting down device")


@dataclass
class JournalEntry:
    message: str
    timestamp: datetime
    cursor: str | None = None


@dataclass
class RdcConfig:
    thread_prefix: str = "rdc"
    title_prefix: str = ""
    body_prefix: str = ""
    timezone: str = "UTC"
    process_grace_seconds: int = 120


@dataclass
class RdcState:
    phase: str = "unknown"  # unknown | starting | auth_required | online | stopped
    device_id: str | None = None
    device_name: str | None = None
    awaiting: str = "none"  # none | url | code
    url: str | None = None
    code: str | None = None
    code_expires_at: str | None = None
    auth_thread_key: str | None = None
    session_invalid: bool = False
    session_restored: bool = False
    process_missing_since: str | None = None
    process_alarm_open: bool = False
    last_seen_at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> RdcState:
        data = dict(data or {})
        known = {name for name in cls.__dataclass_fields__}
        return cls(**{key: value for key, value in data.items() if key in known})


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _label(state: RdcState) -> str:
    return f"Desktop Commander on {state.device_name}" if state.device_name else "Desktop Commander"


def _auth_thread(config: RdcConfig, state: RdcState) -> str:
    return f"{config.thread_prefix}.auth:{state.device_id or 'unknown'}"


def _auth_required_event(config: RdcConfig, state: RdcState, expires_at: datetime) -> NotificationEvent:
    local = expires_at.astimezone(ZoneInfo(config.timezone))
    reason = ("The persisted session was rejected (refresh token already used), so the connector "
        "needs a fresh device authorization.") if state.session_invalid else "The connector is waiting for device authorization."
    return NotificationEvent(
        source=SOURCE, kind="auth_required", severity=Severity.ACTION_REQUIRED.value,
        title=f"{config.title_prefix}{_label(state)} needs a new device code",
        body=f"{config.body_prefix}Enter {state.code} at {state.url} (expires {local.strftime('%H:%M')}). "
            "Remote access stays unavailable until then.",
        detail={"url": state.url, "code": state.code, "expires_at": _iso(expires_at), "device_id": state.device_id,
            "device_name": state.device_name, "reason": reason, "open_url": state.url},
        sensitive_fields=("code", "body"), thread_key=state.auth_thread_key or _auth_thread(config, state),
    )


def apply_journal(state: RdcState, entries: list[JournalEntry], config: RdcConfig, *, now: datetime | None = None,
        ) -> tuple[RdcState, list[NotificationEvent]]:
    """Pure transition over lifecycle lines. Unknown lines change nothing."""
    now = now or datetime.now(UTC)
    events: list[NotificationEvent] = []
    for entry in entries:
        message = entry.message[:MESSAGE_LIMIT].strip()
        state.last_seen_at = _iso(entry.timestamp)
        if RE_STARTING.search(message):
            state.phase, state.awaiting = "starting", "none"
            state.session_invalid = state.session_restored = False
            state.url = state.code = state.code_expires_at = None
            continue
        if (match := RE_PERSISTED.search(message)) or (match := RE_AUTHENTICATED.search(message)):
            state.device_id = match.group(1).lower()
            continue
        if RE_SESSION_INVALID.search(message):
            state.session_invalid = True
            continue
        if RE_SESSION_RESTORED.search(message):
            state.session_restored = True
            continue
        if RE_AUTH_FLOW.search(message):
            state.awaiting = "none"
            continue
        if RE_CODE_RECEIVED.search(message):
            state.awaiting, state.url, state.code = "url", None, None
            continue
        if state.awaiting == "url" and (match := RE_VERIFY_URL.match(message)):
            state.url, state.awaiting = match.group(1), "code"
            continue
        if state.awaiting == "code" and (match := RE_CODE.match(message)):
            state.code, state.awaiting = match.group(1), "expiry"
            continue
        if state.awaiting == "expiry" and (match := RE_EXPIRES.search(message)):
            expires_at = entry.timestamp + timedelta(minutes=int(match.group(1)))
            state.code_expires_at, state.awaiting = _iso(expires_at), "none"
            state.phase = "auth_required"
            state.auth_thread_key = _auth_thread(config, state)
            if state.url and state.code and expires_at > now:
                events.append(_auth_required_event(config, state, expires_at))
            continue
        if RE_ONLINE.search(message) or RE_READY.search(message):
            previous = state.phase
            state.phase = "online"
            if previous == "auth_required":
                events.append(NotificationEvent(source=SOURCE, kind="auth_restored", severity=Severity.RESOLVED.value,
                    title=f"{config.title_prefix}{_label(state)} is back online",
                    body=f"{config.body_prefix}Device authenticated and online; remote access is restored.",
                    detail={"device_id": state.device_id, "device_name": state.device_name, "open_url": "/"},
                    thread_key=state.auth_thread_key or _auth_thread(config, state)))
                state.url = state.code = state.code_expires_at = None
            elif previous == "starting" and state.session_restored:
                events.append(NotificationEvent(source=SOURCE, kind="restarted", severity=Severity.INFO.value,
                    title=f"{config.title_prefix}{_label(state)} restarted and restored its session",
                    body=f"{config.body_prefix}The connector came back without needing a new device code.",
                    detail={"device_id": state.device_id, "device_name": state.device_name, "open_url": "/"},
                    thread_key=f"{config.thread_prefix}.restart"))
            continue
        if match := RE_DEVICE_NAME.search(message):
            state.device_name = match.group(1)
            continue
        if RE_SHUTDOWN.search(message):
            state.phase, state.awaiting = "stopped", "none"
            continue
    return state, events


def apply_liveness(state: RdcState, alive: bool, config: RdcConfig, *, now: datetime | None = None,
        ) -> tuple[RdcState, list[NotificationEvent]]:
    now = now or datetime.now(UTC)
    events: list[NotificationEvent] = []
    thread = f"{config.thread_prefix}.process"
    if alive:
        state.process_missing_since = None
        if state.process_alarm_open:
            state.process_alarm_open = False
            events.append(NotificationEvent(source=SOURCE, kind="process_restored", severity=Severity.RESOLVED.value,
                title=f"{config.title_prefix}{_label(state)} process is running again",
                body=f"{config.body_prefix}The connector process is back.", detail={"open_url": "/"}, thread_key=thread))
        return state, events
    missing_since = _parse_iso(state.process_missing_since)
    if missing_since is None:
        state.process_missing_since = _iso(now)
        return state, events
    if not state.process_alarm_open and (now - missing_since) > timedelta(seconds=config.process_grace_seconds):
        state.process_alarm_open = True
        events.append(NotificationEvent(source=SOURCE, kind="process_missing", severity=Severity.WARNING.value,
            title=f"{config.title_prefix}{_label(state)} process is not running",
            body=f"{config.body_prefix}No connector process has been seen since {missing_since.astimezone(ZoneInfo(config.timezone)).strftime('%H:%M')}. "
                "Atlas does not restart it; check the unit on the host.",
            detail={"missing_since": _iso(missing_since), "open_url": "/"}, thread_key=thread))
    return state, events


def journal_argv(unit: str, uid: int, cursor: str | None) -> list[str]:
    argv = ["journalctl", "-o", "json", "-q", "--no-pager", f"_SYSTEMD_USER_UNIT={unit}", f"_UID={int(uid)}"]
    if cursor:
        argv += ["--after-cursor", cursor]
    else:
        argv += ["--since", "-15min"]
    return argv


def parse_journal_output(raw: bytes) -> tuple[list[JournalEntry], str | None]:
    entries: list[JournalEntry] = []
    cursor: str | None = None
    for line in raw.splitlines():
        if not line.startswith(b"{"):
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        message = record.get("MESSAGE")
        if not isinstance(message, str):
            continue
        try:
            timestamp = datetime.fromtimestamp(int(record.get("__REALTIME_TIMESTAMP", 0)) / 1_000_000, tz=UTC)
        except (TypeError, ValueError):
            timestamp = datetime.now(UTC)
        entry_cursor = record.get("__CURSOR")
        if isinstance(entry_cursor, str):
            cursor = entry_cursor
        entries.append(JournalEntry(message=message[:MESSAGE_LIMIT], timestamp=timestamp, cursor=cursor))
    return entries, cursor


async def read_journal(unit: str, uid: int, cursor: str | None) -> tuple[list[JournalEntry], str | None]:
    """Fixed-argv journal read. On a stale cursor, fall back once to the recent window."""
    for attempt_cursor in (cursor, None) if cursor else (None,):
        process = await asyncio.create_subprocess_exec(*journal_argv(unit, uid, attempt_cursor),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, stdin=asyncio.subprocess.DEVNULL)
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=JOURNAL_TIMEOUT_SECONDS)
        except TimeoutError:
            process.kill()
            raise RuntimeError("journalctl timed out") from None
        if process.returncode == 0:
            if len(stdout) > JOURNAL_OUTPUT_LIMIT:
                logger.warning("RDC journal window exceeded %d bytes; truncating", JOURNAL_OUTPUT_LIMIT)
                stdout = stdout[-JOURNAL_OUTPUT_LIMIT:]
            entries, new_cursor = parse_journal_output(stdout)
            return entries, new_cursor or attempt_cursor
        if attempt_cursor is not None and b"cursor" in stderr.lower():
            continue
        raise RuntimeError(f"journalctl failed with status {process.returncode}")
    return [], cursor


def process_alive(uid: int, *, proc_root: Path = Path("/proc")) -> bool:
    try:
        candidates = [entry for entry in proc_root.iterdir() if entry.name.isdigit()]
    except OSError:
        return False
    for entry in candidates:
        try:
            if entry.stat().st_uid != uid:
                continue
            argv = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if any(b"desktop-commander" in arg for arg in argv) and b"remote" in argv:
            return True
    return False


def config_from_settings(settings: Settings, *, replay: bool = False) -> RdcConfig:
    if replay:
        return RdcConfig(thread_prefix="rdc.replay", title_prefix="[replay test] ", body_prefix="[replay test] ",
            timezone=settings.owner_timezone, process_grace_seconds=settings.rdc_monitor_process_grace_seconds)
    return RdcConfig(timezone=settings.owner_timezone, process_grace_seconds=settings.rdc_monitor_process_grace_seconds)


async def rdc_monitor_once(settings: Settings, factory, *, journal_reader=None, liveness=None,
        monitor_id: str = MONITOR_ID, config: RdcConfig | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Read the journal since the stored cursor, apply transitions, emit, and persist cursor + state atomically."""
    config = config or config_from_settings(settings)
    now = now or datetime.now(UTC)
    async with factory() as session:
        row = await session.get(HostMonitorStateRow, monitor_id, with_for_update=True)
        if row is None:
            row = HostMonitorStateRow(monitor_id=monitor_id, cursor=None, state={})
            session.add(row)
            await session.flush()
        state = RdcState.from_json(row.state)
        reader = journal_reader or (lambda cursor: read_journal(settings.rdc_monitor_unit, settings.rdc_monitor_uid, cursor))
        entries, cursor = await reader(row.cursor)
        state, events = apply_journal(state, entries, config, now=now)
        alive = liveness() if liveness is not None else process_alive(settings.rdc_monitor_uid)
        state, liveness_events = apply_liveness(state, alive, config, now=now)
        events.extend(liveness_events)
        service = NotificationService(session, repeat_minutes=settings.push_repeat_minutes)
        emitted = 0
        for event in events:
            if await service.emit(event, now=now) is not None:
                emitted += 1
        row.cursor = cursor or row.cursor
        row.state = state.to_json()
        await session.commit()
    return {"entries": len(entries), "events": len(events), "emitted": emitted, "phase": state.phase, "alive": alive}


async def rdc_monitor_loop(settings: Settings) -> None:
    from atlas.db import get_session_factory

    while True:
        try:
            await rdc_monitor_once(settings, get_session_factory())
        except Exception:
            logger.exception("RDC monitor pass failed")
        await asyncio.sleep(max(5, settings.rdc_monitor_poll_seconds))


def load_replay_entries(path: Path, *, now: datetime | None = None) -> list[JournalEntry]:
    """Recorded entries rebased so the last one lands at now; device codes stay unexpired."""
    now = now or datetime.now(UTC)
    raw_entries, _ = parse_journal_output(path.read_bytes())
    if not raw_entries:
        return []
    shift = now - raw_entries[-1].timestamp
    return [JournalEntry(message=entry.message, timestamp=entry.timestamp + shift) for entry in raw_entries]


async def _replay(path: Path) -> dict[str, Any]:
    from atlas.config import get_settings
    from atlas.db import get_session_factory

    settings = get_settings()
    entries = load_replay_entries(path)

    async def reader(_cursor):
        return entries, None

    return await rdc_monitor_once(settings, get_session_factory(), journal_reader=reader, liveness=lambda: True,
        monitor_id="rdc-replay", config=config_from_settings(settings, replay=True))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Desktop Commander connector monitor")
    parser.add_argument("--replay", type=Path, help="JSONL journal export to feed through the real pipeline as a replay test")
    args = parser.parse_args(argv)
    if args.replay is None:
        parser.error("--replay <file> is required; the live monitor runs inside the Atlas API process")
    print(json.dumps(asyncio.run(_replay(args.replay)), sort_keys=True))


if __name__ == "__main__":
    main()
