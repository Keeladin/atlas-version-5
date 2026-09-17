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


def test_cancel_escalates_to_sigkill_if_process_group_survives(monkeypatch):
    alive = iter([True, True, False])
    monkeypatch.setattr(coding, "_process_group_alive", lambda **kwargs: next(alive))
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




def test_process_group_liveness_keeps_orphaned_children_visible(monkeypatch):
    monkeypatch.setattr(coding, "_process_identity", lambda pid: None)
    monkeypatch.setattr(
        coding,
        "_process_group_members",
        lambda process_group: [(7777, "S", "child-birth")],
    )
    assert coding._process_group_alive(
        pid=4321, process_group=4321, expected_start_time="leader-birth"
    ) is True

    monkeypatch.setattr(coding, "_process_identity", lambda pid: ("S", "reused-birth"))
    assert coding._process_group_alive(
        pid=4321, process_group=4321, expected_start_time="leader-birth"
    ) is False


def test_cancel_before_start_creates_tombstone_and_blocks_late_launch(tmp_path, monkeypatch):
    monkeypatch.setattr(coding, "STATE_DIR", tmp_path / "state")
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(coding, "_validate_repo", lambda value: repo)
    launched = []
    monkeypatch.setattr(coding.subprocess, "Popen", lambda *args, **kwargs: launched.append(True))
    session_id = "12345678-1234-1234-1234-123456789abc"

    cancelled = coding.cancel_session({"session_id": session_id})
    assert cancelled["status"] == "cancelled"
    assert coding._load(session_id)["cancelled_before_start"] is True

    with pytest.raises(ValueError, match="cancelled before launch"):
        coding.start_session({
            "session_id": session_id,
            "repo": str(repo),
            "prompt": "Do not start after cancellation",
        })
    assert launched == []


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


@pytest.mark.parametrize("resume", [False, True])
def test_codex_launch_is_noninteractive_under_outer_systemd_sandbox(tmp_path, monkeypatch, resume):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(coding, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(coding, "_validate_repo", lambda value: repo)
    monkeypatch.setattr(coding, "_codex_binary", lambda: tmp_path / "codex")
    monkeypatch.setattr(coding, "_process_identity", lambda pid: ("S", "birth-4321"))
    monkeypatch.setattr(coding, "_process_group_alive", lambda **kwargs: False)
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
        "codex_thread_id": "existing-thread",
    }

    coding._launch(record, "Implement the task", resume=resume)

    command = captured["command"]
    assert command[1:5] == [
        "--ask-for-approval",
        "never",
        "--sandbox",
        "danger-full-access",
    ]
    assert command[5:7] == ["exec", "--json"]
    assert "--dangerously-bypass-approvals-and-sandbox" not in command
    assert ("resume" in command) is resume
    assert record["runs"][0]["proc_start_time"] == "birth-4321"
    assert record["status"] == "interrupted"


def test_resume_refuses_implicit_new_codex_thread(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(coding, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(coding, "_validate_repo", lambda value: repo)
    monkeypatch.setattr(coding, "_codex_binary", lambda: tmp_path / "codex")
    monkeypatch.setattr(coding, "_process_identity", lambda pid: ("S", "birth-4321"))
    monkeypatch.setattr(coding, "_process_group_alive", lambda **kwargs: True)
    monkeypatch.setattr(
        coding,
        "_events",
        lambda path: [{"type": "thread.started", "thread_id": "unexpected-thread"}],
    )
    terminated = []
    monkeypatch.setattr(
        coding,
        "_terminate_process_group",
        lambda **kwargs: terminated.append(kwargs),
    )

    class Process:
        pid = 4321

    monkeypatch.setattr(coding.subprocess, "Popen", lambda command, **kwargs: Process())
    session_id = "12345678-1234-1234-1234-123456789abc"
    record = {
        "session_id": session_id,
        "codex_thread_id": "expected-thread",
        "repo": str(repo),
        "runs": [],
    }

    with pytest.raises(RuntimeError, match="different thread id"):
        coding._launch(record, "Continue the task", resume=True)

    assert terminated and terminated[0]["pid"] == 4321
    persisted = coding._load(session_id)
    assert persisted["status"] == "failed"
    assert persisted["codex_thread_id"] == "expected-thread"
    assert persisted["runs"][0]["resume_thread_mismatch"] == {
        "expected": "expected-thread",
        "observed": "unexpected-thread",
    }


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
