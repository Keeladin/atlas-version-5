import json
from pathlib import Path
from types import SimpleNamespace

from atlas.integrations.google_workspace import GoogleWorkspaceService


def test_gmail_send_uses_exact_prepared_message(monkeypatch, tmp_path: Path) -> None:
    seen = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        return SimpleNamespace(returncode=0, stdout=json.dumps({"id": "sent-1"}), stderr="")

    monkeypatch.setattr("atlas.integrations.google_workspace.subprocess.run", fake_run)
    service = GoogleWorkspaceService(Path("/bin/gws"), None, tmp_path, tmp_path)
    result = service.gmail_send(to="owner@example.com", subject="Subject", body="Body")

    assert result == {"id": "sent-1"}
    assert seen["command"] == [
        "/bin/gws", "gmail", "+send", "--to", "owner@example.com",
        "--subject", "Subject", "--body", "Body", "--format", "json",
    ]


def test_calendar_agenda_is_read_only_helper(monkeypatch, tmp_path: Path) -> None:
    seen = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        return SimpleNamespace(returncode=0, stdout=json.dumps({"events": []}), stderr="")

    monkeypatch.setattr("atlas.integrations.google_workspace.subprocess.run", fake_run)
    service = GoogleWorkspaceService(Path("/bin/gws"), None, tmp_path, tmp_path)
    result = service.calendar_agenda(days=3, timezone="Africa/Johannesburg")

    assert result == {"events": []}
    assert seen["command"] == [
        "/bin/gws", "calendar", "+agenda", "--days", "3", "--format", "json",
        "--timezone", "Africa/Johannesburg",
    ]


def test_calendar_create_preserves_exact_prepared_event(monkeypatch, tmp_path: Path) -> None:
    seen = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        return SimpleNamespace(returncode=0, stdout=json.dumps({"id": "event-1"}), stderr="")

    monkeypatch.setattr("atlas.integrations.google_workspace.subprocess.run", fake_run)
    service = GoogleWorkspaceService(Path("/bin/gws"), None, tmp_path, tmp_path)
    result = service.calendar_event_create(
        summary="Maintenance review", start="2026-09-07T09:00:00+02:00", end="2026-09-07T09:30:00+02:00",
        attendees=["owner@example.com"], meet=True,
    )

    assert result == {"id": "event-1"}
    assert seen["command"] == [
        "/bin/gws", "calendar", "+insert", "--calendar", "primary", "--summary", "Maintenance review",
        "--start", "2026-09-07T09:00:00+02:00", "--end", "2026-09-07T09:30:00+02:00", "--format", "json",
        "--attendee", "owner@example.com", "--meet",
    ]
