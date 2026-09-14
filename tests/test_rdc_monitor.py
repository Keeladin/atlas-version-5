"""Desktop Commander monitor: pure parser/state machine over recorded journal lines."""
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from atlas.monitors.rdc import (
    JournalEntry,
    RdcConfig,
    RdcState,
    apply_journal,
    apply_liveness,
    journal_argv,
    load_replay_entries,
    parse_journal_output,
    process_alive,
)

FIXTURE = Path(__file__).parent / "fixtures" / "rdc_journal_auth_flow.jsonl"
CONFIG = RdcConfig(timezone="Africa/Johannesburg", process_grace_seconds=120)
NOW = datetime(2026, 9, 13, 6, 46, 40, tzinfo=UTC)


def _entries() -> list[JournalEntry]:
    return load_replay_entries(FIXTURE, now=NOW)


def _lines(*messages: str, start: datetime = NOW) -> list[JournalEntry]:
    return [JournalEntry(message=message, timestamp=start + timedelta(seconds=index)) for index, message in enumerate(messages)]


def test_recorded_auth_flow_yields_one_code_event_and_one_resolution() -> None:
    state, events = apply_journal(RdcState(), _entries(), CONFIG, now=NOW - timedelta(minutes=1))
    kinds = [event.kind for event in events]
    assert kinds == ["auth_required", "auth_restored"]
    auth = events[0]
    assert auth.severity == "action_required"
    assert auth.thread_key == "rdc.auth:11111111-2222-4333-8444-555555555555"
    assert auth.detail["code"] == "AB12-CD34"
    assert auth.detail["url"] == "https://mcp.desktopcommander.app/device/verify"
    assert auth.detail["open_url"] == auth.detail["url"]
    assert "AB12-CD34" in auth.body and "AB12-CD34" not in auth.title
    assert set(auth.sensitive_fields) == {"code", "body"}
    expires = datetime.fromisoformat(auth.detail["expires_at"])
    code_line = next(entry for entry in _entries() if "Code expires" in entry.message)
    assert expires == code_line.timestamp + timedelta(minutes=15)
    assert "refresh token already used" in auth.detail["reason"]
    assert events[1].severity == "resolved" and events[1].thread_key == auth.thread_key
    assert state.phase == "online" and state.device_name == "ubuntuserver" and state.code is None


def test_giant_tool_output_lines_change_nothing_and_never_persist() -> None:
    noise = " " + json.dumps({"content": [{"type": "text", "text": "SECRET-PAYLOAD " * 3000}]})
    entries = _entries()
    entries.insert(6, JournalEntry(message=noise, timestamp=entries[5].timestamp))
    entries.insert(0, JournalEntry(message="🔧 Received tool call abc: start_process {\"command\":\"ls\"}", timestamp=entries[0].timestamp))
    state, events = apply_journal(RdcState(), entries, CONFIG, now=NOW - timedelta(minutes=1))
    assert [event.kind for event in events] == ["auth_required", "auth_restored"]
    serialized = json.dumps(state.to_json())
    assert "SECRET-PAYLOAD" not in serialized and "start_process" not in serialized


def test_renewed_code_reuses_the_same_thread() -> None:
    flow = [entry for entry in _entries() if "authenticated" not in entry.message and "online" not in entry.message
        and "Device ready" not in entry.message and "Device ID:" not in entry.message and "Device Name" not in entry.message
        and "Presence" not in entry.message]
    state, first = apply_journal(RdcState(), flow, CONFIG, now=NOW - timedelta(minutes=1))
    renewed = [JournalEntry(message=entry.message.replace("AB12-CD34", "ZZ99-YY88"), timestamp=entry.timestamp + timedelta(minutes=17))
        for entry in flow]
    state, second = apply_journal(state, renewed, CONFIG, now=NOW + timedelta(minutes=16))
    assert len(first) == 1 and len(second) == 1
    assert second[0].thread_key == first[0].thread_key
    assert second[0].detail["code"] == "ZZ99-YY88" and state.code == "ZZ99-YY88"


def test_restart_with_restored_session_is_info_only() -> None:
    state, events = apply_journal(RdcState(), _lines(
        "🚀 Starting MCP Device...",
        "💾 Found persisted session for device 11111111-2222-4333-8444-555555555555",
        "   - ✅ Session restored",
        "🔌 Device marked as online",
        "   - Device Name:  ubuntuserver",
        "🔌 Device marked as online",
    ), CONFIG, now=NOW + timedelta(minutes=1))
    assert [event.kind for event in events] == ["restarted"]
    assert events[0].severity == "info" and events[0].thread_key == "rdc.restart"
    assert state.phase == "online"


def test_expired_code_on_replay_is_not_announced() -> None:
    state, events = apply_journal(RdcState(), _entries()[:12], CONFIG, now=NOW + timedelta(hours=2))
    assert events == []
    assert state.phase == "auth_required" and state.code == "AB12-CD34"


def test_liveness_grace_then_warning_then_resolution() -> None:
    state = RdcState(phase="online", device_name="ubuntuserver")
    state, events = apply_liveness(state, False, CONFIG, now=NOW)
    assert events == [] and state.process_missing_since is not None
    state, events = apply_liveness(state, False, CONFIG, now=NOW + timedelta(seconds=60))
    assert events == []
    state, events = apply_liveness(state, False, CONFIG, now=NOW + timedelta(seconds=121))
    assert [event.kind for event in events] == ["process_missing"]
    assert events[0].severity == "warning" and events[0].thread_key == "rdc.process"
    assert "No connector process has been seen" in events[0].body
    state, events = apply_liveness(state, False, CONFIG, now=NOW + timedelta(seconds=300))
    assert events == []
    state, events = apply_liveness(state, True, CONFIG, now=NOW + timedelta(seconds=400))
    assert [event.kind for event in events] == ["process_restored"] and events[0].severity == "resolved"
    assert state.process_missing_since is None and state.process_alarm_open is False


def test_journal_argv_is_fixed_and_cursor_conditional() -> None:
    without = journal_argv("desktop-commander.service", 1000, None)
    with_cursor = journal_argv("desktop-commander.service", 1000, "s=abc;i=1")
    assert without[:7] == ["journalctl", "-o", "json", "-q", "--no-pager", "_SYSTEMD_USER_UNIT=desktop-commander.service", "_UID=1000"]
    assert "--since" in without and "--after-cursor" not in without
    assert with_cursor[-2:] == ["--after-cursor", "s=abc;i=1"] and "--since" not in with_cursor
    system = journal_argv("desktop-commander.service", 1000, None, scope="system")
    assert "_SYSTEMD_UNIT=desktop-commander.service" in system and not any(item.startswith("_UID=") for item in system)


def test_parse_journal_output_keeps_only_message_timestamp_and_cursor() -> None:
    raw = b"\n".join([
        b"-- No entries --",
        json.dumps({"MESSAGE": "hello", "__REALTIME_TIMESTAMP": "1789281600000000", "__CURSOR": "c1", "_CMDLINE": "secret"}).encode(),
        json.dumps({"MESSAGE": [1, 2, 3], "__REALTIME_TIMESTAMP": "1789281601000000", "__CURSOR": "c2"}).encode(),
        b"not json",
        json.dumps({"MESSAGE": "x" * 2000, "__REALTIME_TIMESTAMP": "1789281602000000", "__CURSOR": "c3"}).encode(),
    ])
    entries, cursor = parse_journal_output(raw)
    assert [entry.message[:5] for entry in entries] == ["hello", "xxxxx"]
    assert len(entries[1].message) == 512
    assert cursor == "c3"
    assert entries[0].timestamp == datetime.fromtimestamp(1789281600, tz=UTC)


def test_state_roundtrip_ignores_unknown_keys() -> None:
    state = RdcState.from_json({"phase": "online", "device_name": "ubuntuserver", "unexpected": 1})
    assert state.phase == "online" and state.to_json()["device_name"] == "ubuntuserver"


def test_process_alive_matches_connector_argv_for_uid(tmp_path) -> None:
    proc = tmp_path / "proc"
    (proc / "123").mkdir(parents=True)
    (proc / "123" / "cmdline").write_bytes(b"node\0/home/jaco/.local/node/bin/desktop-commander\0remote\0")
    (proc / "notpid").mkdir()
    import os

    uid = os.getuid()
    assert process_alive(uid, proc_root=proc) is True
    assert process_alive(uid + 1, proc_root=proc) is False
    (proc / "123" / "cmdline").write_bytes(b"node\0something-else\0")
    assert process_alive(uid, proc_root=proc) is False
