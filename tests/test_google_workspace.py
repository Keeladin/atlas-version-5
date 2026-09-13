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


def test_restart_with_managed_google_bundle_registers_drive_gmail_and_calendar(monkeypatch, tmp_path: Path) -> None:
    from atlas.capabilities.factory import build_capability_runtime
    from atlas.config import Settings
    from atlas.control.connections import (
        apply_managed_overrides,
        save_google_connection,
    )
    from atlas.registry.service import build_phase0_registry

    command = tmp_path / "gws"
    command.write_text("stub")
    workspace = tmp_path / "workspace"
    projects = tmp_path / "projects"
    workspace.mkdir(); projects.mkdir()
    settings = Settings(state_dir=tmp_path, gws_command=command, gws_credentials_file=None,
        workspace_root=workspace, projects_root=projects, openai_api_key_file=None)
    save_google_connection(settings, {"type": "authorized_user", "client_id": "client",
        "client_secret": "secret", "refresh_token": "refresh"})
    restarted = apply_managed_overrides(Settings(state_dir=tmp_path, gws_command=command,
        gws_credentials_file=None, workspace_root=workspace, projects_root=projects, openai_api_key_file=None))

    class FakeGoogle:
        def __init__(self, command, credentials_file, config_dir, workspace_dir):
            assert credentials_file.parent == config_dir
        def auth_status(self):
            return {"scopes": ["https://www.googleapis.com/auth/drive", "https://www.googleapis.com/auth/gmail.modify", "https://www.googleapis.com/auth/calendar"]}

    monkeypatch.setattr("atlas.capabilities.factory.GoogleWorkspaceService", FakeGoogle)
    registry = build_phase0_registry(restarted)
    build_capability_runtime(restarted, registry)
    operation_ids = {item.id for item in registry.operations()}
    assert {"drive.files.list", "gmail.messages.search", "calendar.agenda"} <= operation_ids
