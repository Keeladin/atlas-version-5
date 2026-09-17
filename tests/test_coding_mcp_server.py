import json

import pytest
from atlas.integrations import coding_mcp_server as coding
from atlas.integrations import coding_mcp_socket_server as socket_server


def test_coding_repo_must_be_inside_owner_approved_root(tmp_path, monkeypatch):
    allowed = tmp_path / "Projects"
    allowed.mkdir()
    repo = allowed / "Atlas V5"
    repo.mkdir()
    outside = tmp_path / "Elsewhere"
    outside.mkdir()
    monkeypatch.setenv("ATLAS_CODING_ROOTS", str(allowed))

    assert coding._validate_repo(repo) == repo.resolve()
    with pytest.raises(ValueError, match="outside approved coding roots"):
        coding._validate_repo(outside)


def test_coding_repo_symlink_cannot_escape_root(tmp_path, monkeypatch):
    allowed = tmp_path / "Workspace"
    allowed.mkdir()
    outside = tmp_path / "private"
    outside.mkdir()
    link = allowed / "escape"
    link.symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("ATLAS_CODING_ROOTS", str(allowed))

    with pytest.raises(ValueError, match="outside approved coding roots"):
        coding._validate_repo(link)


def test_coding_roots_use_platform_path_separator(tmp_path, monkeypatch):
    first = tmp_path / "Projects"
    second = tmp_path / "Workspace"
    first.mkdir()
    second.mkdir()
    project = second / "Morning"
    project.mkdir()
    monkeypatch.setenv("ATLAS_CODING_ROOTS", f"{first}{coding.os.pathsep}{second}")

    roots = coding._allowed_roots()
    assert roots == (first.resolve(), second.resolve())
    assert coding._validate_repo(project) == project.resolve()


def test_pid_liveness_is_fenced_by_process_birth(monkeypatch):
    monkeypatch.setattr(coding, "_process_identity", lambda pid: ("S", "222"))
    assert coding._alive(4321, "222") is True
    assert coding._alive(4321, "111") is False
    monkeypatch.setattr(coding, "_process_identity", lambda pid: ("Z", "222"))
    assert coding._alive(4321, "222") is False


def test_dead_codex_child_without_events_is_recoverable():
    assert coding._event_status([], running=False) == "interrupted"


def test_cancel_escalates_to_sigkill_if_same_process_survives(monkeypatch):
    alive = iter([True, True, False])
    monkeypatch.setattr(coding, "_alive", lambda pid, expected_start_time=None: next(alive))
    signals = []
    monkeypatch.setattr(coding.os, "killpg", lambda group, sig: signals.append((group, sig)))

    coding._terminate_process_group(
        pid=4321,
        process_group=4321,
        expected_start_time="birth-4321",
        term_grace_seconds=0,
        kill_grace_seconds=0,
    )

    assert signals == [
        (4321, coding.signal.SIGTERM),
        (4321, coding.signal.SIGKILL),
    ]


def test_socket_transport_reports_runtime_error_without_crashing(monkeypatch):
    def fail(arguments):
        raise RuntimeError("cancel verification failed")

    monkeypatch.setitem(socket_server.HANDLERS, "explode", fail)
    response = socket_server._response({
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {"name": "explode", "arguments": {}},
    })

    assert response is not None
    result = response["result"]
    assert result["isError"] is True
    assert result["structuredContent"] == {
        "error": "cancel verification failed",
        "type": "RuntimeError",
    }


def test_codex_launch_is_noninteractive_but_keeps_workspace_sandbox(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(coding, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(coding, "_validate_repo", lambda value: repo)
    monkeypatch.setattr(coding, "_codex_binary", lambda: tmp_path / "codex")
    monkeypatch.setattr(coding, "_process_identity", lambda pid: ("S", "birth-4321"))
    monkeypatch.setattr(coding, "_alive", lambda pid, expected_start_time=None: False)
    captured = {}

    class Process:
        pid = 4321

    def fake_popen(command, **kwargs):
        captured["command"] = command
        return Process()

    monkeypatch.setattr(coding.subprocess, "Popen", fake_popen)
    record = {
        "session_id": "12345678-1234-1234-1234-123456789abc",
        "repo": str(repo),
        "runs": [],
    }

    coding._launch(record, "Implement the task", resume=False)

    command = captured["command"]
    assert command[1:3] == ["exec", "--json"]
    assert command[command.index("--sandbox") + 1] == "workspace-write"
    assert command[command.index("--ask-for-approval") + 1] == "never"
    assert "--dangerously-bypass-approvals-and-sandbox" not in command
    assert record["runs"][0]["proc_start_time"] == "birth-4321"
    assert record["status"] == "interrupted"


def test_cancel_does_not_rewrite_completed_session(tmp_path, monkeypatch):
    monkeypatch.setattr(coding, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(coding, "_process_identity", lambda pid: None)
    session_id = "12345678-1234-1234-1234-123456789abc"
    log_path = tmp_path / "turn.jsonl"
    log_path.write_text(json.dumps({"type": "turn.completed"}) + "\n")
    record = {
        "session_id": session_id,
        "status": "completed",
        "runs": [{
            "turn": 1,
            "pid": 9876,
            "process_group": 9876,
            "proc_start_time": "old-birth",
            "status": "completed",
            "log_path": str(log_path),
            "finished_at": "2026-09-16T00:00:00+00:00",
        }],
    }
    coding._atomic_json(coding._session_path(session_id), record)

    result = coding.cancel_session({"session_id": session_id})

    assert result["status"] == "completed"
    assert coding._load(session_id)["status"] == "completed"
