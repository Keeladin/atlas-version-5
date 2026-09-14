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


def test_gmail_draft_reply_and_forward_use_native_helpers(monkeypatch, tmp_path: Path) -> None:
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout=json.dumps({"id": "ok"}), stderr="")

    monkeypatch.setattr("atlas.integrations.google_workspace.subprocess.run", fake_run)
    service = GoogleWorkspaceService(Path("/bin/gws"), None, tmp_path, tmp_path)

    service.gmail_draft(to="a@example.com", subject="Draft", body="Body", cc="c@example.com")
    service.gmail_reply(message_id="msg-1", body="Reply", reply_all=True, draft=True)
    service.gmail_forward(message_id="msg-2", to="b@example.com", body="FYI", include_original_attachments=False, draft=True)

    assert commands[0] == [
        "/bin/gws", "gmail", "+send", "--to", "a@example.com", "--subject", "Draft", "--body", "Body",
        "--format", "json", "--draft", "--cc", "c@example.com",
    ]
    assert commands[1] == [
        "/bin/gws", "gmail", "+reply-all", "--message-id", "msg-1", "--body", "Reply", "--format", "json", "--draft",
    ]
    assert commands[2] == [
        "/bin/gws", "gmail", "+forward", "--message-id", "msg-2", "--to", "b@example.com", "--format", "json",
        "--body", "FYI", "--no-original-attachments", "--draft",
    ]


def test_gmail_archive_trash_restore_and_delete_use_message_api(monkeypatch, tmp_path: Path) -> None:
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout=json.dumps({"id": "msg-1"}), stderr="")

    monkeypatch.setattr("atlas.integrations.google_workspace.subprocess.run", fake_run)
    service = GoogleWorkspaceService(Path("/bin/gws"), None, tmp_path, tmp_path)

    service.gmail_archive("msg-1")
    service.gmail_trash("msg-1")
    service.gmail_restore("msg-1")
    service.gmail_delete_permanently("msg-1")

    assert commands[0] == [
        "/bin/gws", "gmail", "users", "messages", "modify", "--params", '{"userId":"me","id":"msg-1"}',
        "--json", '{"removeLabelIds":["INBOX"]}', "--format", "json",
    ]
    for command_name, command in zip(("trash", "untrash", "delete"), commands[1:], strict=True):
        assert command == [
            "/bin/gws", "gmail", "users", "messages", command_name, "--params", '{"userId":"me","id":"msg-1"}',
            "--format", "json",
        ]


def test_gmail_label_changes_resolve_names_to_ids(monkeypatch, tmp_path: Path) -> None:
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        if command[2:5] == ["users", "labels", "list"]:
            payload = {"labels": [{"id": "STARRED", "name": "STARRED"}, {"id": "Label_9", "name": "Engineering"}]}
        else:
            payload = {"id": "msg-1"}
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr("atlas.integrations.google_workspace.subprocess.run", fake_run)
    service = GoogleWorkspaceService(Path("/bin/gws"), None, tmp_path, tmp_path)

    service.gmail_modify_labels(message_id="msg-1", add_labels=["Engineering"], remove_labels=["starred"])

    assert commands[-1] == [
        "/bin/gws", "gmail", "users", "messages", "modify", "--params", '{"userId":"me","id":"msg-1"}',
        "--json", '{"addLabelIds":["Label_9"],"removeLabelIds":["STARRED"]}', "--format", "json",
    ]
