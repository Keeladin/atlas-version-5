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
