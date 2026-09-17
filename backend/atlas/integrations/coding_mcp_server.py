"""Policy-neutral MCP bridge from Atlas to Codex CLI.

Atlas owns authority. This server only validates technical inputs, manages Codex
process/session lifecycle, and reports structured execution state. It deliberately
contains no approval or permission policy.
"""
from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

PROTOCOL_VERSION = "2025-06-18"
SERVER_VERSION = "0.1.0"
CODEX_BIN = os.environ.get("ATLAS_CODEX_BIN", "/home/jaco/.local/node/bin/codex")
STATE_DIR = Path(
    os.environ.get("ATLAS_CODING_STATE_DIR", "/home/jaco/.local/state/atlas-v5/coding")
).expanduser()
DEFAULT_ROOTS = "/home/jaco/Projects:/home/jaco/Workspace"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _reply(request_id: Any, *, result: dict[str, Any] | None = None,
           error: dict[str, Any] | None = None) -> None:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result or {}
    sys.stdout.write(json.dumps(payload, separators=(",", ":"), default=str) + "\n")
    sys.stdout.flush()


def _tool_result(value: Any, *, error: bool = False) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False, default=str)}],
        "structuredContent": value,
        "isError": error,
    }


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n")
    os.replace(temporary, path)


def _session_path(session_id: str) -> Path:
    if not session_id or any(ch not in "0123456789abcdef-" for ch in session_id.lower()):
        raise ValueError("session_id must be a UUID-like identifier")
    return STATE_DIR / "sessions" / f"{session_id}.json"


def _load(session_id: str) -> dict[str, Any]:
    path = _session_path(session_id)
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise ValueError(f"Unknown coding session: {session_id}") from exc
    if not isinstance(value, dict):
        raise TypeError(f"Coding session state is malformed: {session_id}")
    return value


@contextmanager
def _locked(session_id: str) -> Iterator[None]:
    lock_path = _session_path(session_id).with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield


def _process_identity(pid: int | None) -> tuple[str, str] | None:
    """Return Linux process state + starttime so stale PIDs cannot target a new process."""
    if not pid:
        return None
    try:
        raw = Path(f"/proc/{int(pid)}/stat").read_text()
        _, tail = raw.rsplit(")", 1)
        fields = tail.strip().split()
        if len(fields) <= 19:
            return None
        return fields[0], fields[19]
    except (OSError, ValueError):
        return None


def _alive(pid: int | None, expected_start_time: str | None = None) -> bool:
    identity = _process_identity(pid)
    if identity is None or identity[0] == "Z":
        return False
    return expected_start_time is None or identity[1] == str(expected_start_time)


def _terminate_process_group(
    *,
    pid: int,
    process_group: int,
    expected_start_time: str | None,
    term_grace_seconds: float = 2.0,
    kill_grace_seconds: float = 1.0,
) -> None:
    """Stop the exact Codex process group and verify it cannot keep mutating the repo."""
    if not _alive(pid, expected_start_time):
        return
    try:
        os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        return

    deadline = time.monotonic() + max(0.0, term_grace_seconds)
    while time.monotonic() < deadline:
        if not _alive(pid, expected_start_time):
            return
        time.sleep(0.05)

    if not _alive(pid, expected_start_time):
        return
    try:
        os.killpg(process_group, signal.SIGKILL)
    except ProcessLookupError:
        return

    deadline = time.monotonic() + max(0.0, kill_grace_seconds)
    while time.monotonic() < deadline:
        if not _alive(pid, expected_start_time):
            return
        time.sleep(0.05)
    if _alive(pid, expected_start_time):
        raise RuntimeError("Codex process group remained alive after SIGKILL")


def _events(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    result: list[dict[str, Any]] = []
    try:
        for raw in path.read_text(errors="replace").splitlines():
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                result.append(value)
    except OSError:
        return []
    return result


def _thread_id(events: list[dict[str, Any]]) -> str | None:
    for event in events:
        if event.get("type") == "thread.started" and isinstance(event.get("thread_id"), str):
            return event["thread_id"]
    return None


def _last_agent_text(events: list[dict[str, Any]]) -> str | None:
    for event in reversed(events):
        item = event.get("item")
        if (
            event.get("type") == "item.completed"
            and isinstance(item, dict)
            and item.get("type") == "agent_message"
            and isinstance(item.get("text"), str)
        ):
            return item["text"]
    return None


def _event_status(events: list[dict[str, Any]], running: bool) -> str:
    if running:
        return "running"
    for event in reversed(events):
        if event.get("type") == "turn.completed":
            return "completed"
        if event.get("type") in {"turn.failed", "error"}:
            return "failed"
    # A launched child that is no longer alive must never remain "starting".
    # Treat a no-output exit as interrupted so Atlas can resume/retry the thread.
    return "interrupted"


def _refresh(record: dict[str, Any]) -> dict[str, Any]:
    runs = record.get("runs") if isinstance(record.get("runs"), list) else []
    if not runs:
        return record
    run = runs[-1]
    running = _alive(run.get("pid"), run.get("proc_start_time"))
    events = _events(Path(str(run.get("log_path") or "")))
    thread_id = _thread_id(events)
    if thread_id:
        record["codex_thread_id"] = thread_id
    status = _event_status(events, running)
    run["status"] = status
    if not running and status not in {"starting", "running"} and not run.get("finished_at"):
        run["finished_at"] = _now()
    record["status"] = status
    record["updated_at"] = _now()
    if events:
        run["last_event_type"] = str(events[-1].get("type") or "unknown")
    text = _last_agent_text(events)
    if text is not None:
        run["result"] = text
    return record


def _allowed_roots() -> tuple[Path, ...]:
    raw = os.environ.get("ATLAS_CODING_ROOTS", DEFAULT_ROOTS)
    roots: list[Path] = []
    for value in raw.split(os.pathsep):
        value = value.strip()
        if not value:
            continue
        root = Path(value).expanduser().resolve()
        if root not in roots:
            roots.append(root)
    if not roots:
        raise ValueError("ATLAS_CODING_ROOTS must contain at least one approved root")
    return tuple(roots)


def _validate_repo(value: Any) -> Path:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("repo is required")
    repo = Path(raw).expanduser().resolve()
    if not repo.is_dir():
        raise ValueError(f"Repository/work directory does not exist: {repo}")
    roots = _allowed_roots()
    if not any(repo == root or repo.is_relative_to(root) for root in roots):
        allowed = ", ".join(str(root) for root in roots)
        raise ValueError(f"Repository/work directory is outside approved coding roots: {repo} (allowed: {allowed})")
    return repo


def _codex_binary() -> Path:
    path = Path(CODEX_BIN).expanduser().resolve()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError(f"Codex CLI is unavailable or not executable: {path}")
    return path


def _launch(record: dict[str, Any], prompt: str, *, resume: bool) -> dict[str, Any]:
    if not prompt.strip():
        raise ValueError("prompt is required")
    repo = _validate_repo(record["repo"])
    session_id = str(record["session_id"])
    turn = len(record.get("runs") or []) + 1
    run_dir = STATE_DIR / "runs" / session_id
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / f"turn-{turn:04d}.jsonl"
    stderr_path = run_dir / f"turn-{turn:04d}.stderr.log"

    command = [
        str(_codex_binary()),
        "--ask-for-approval",
        "never",
        "--sandbox",
        "workspace-write",
        "exec",
        "--json",
        "-C",
        str(repo),
    ]
    expected_thread_id: str | None = None
    if resume:
        expected_thread_id = str(record.get("codex_thread_id") or "").strip()
        if not expected_thread_id:
            raise ValueError("Codex thread id is not available yet; wait for the current turn to start")
        command.extend(["resume", expected_thread_id])
    command.append(prompt)

    stdout_handle = log_path.open("ab", buffering=0)
    stderr_handle = stderr_path.open("ab", buffering=0)
    try:
        process = subprocess.Popen(
            command,
            cwd=repo,
            stdin=subprocess.DEVNULL,
            stdout=stdout_handle,
            stderr=stderr_handle,
            start_new_session=True,
            close_fds=True,
            env=os.environ.copy(),
        )
    finally:
        stdout_handle.close()
        stderr_handle.close()

    identity = _process_identity(process.pid)
    proc_start_time = identity[1] if identity is not None else None
    run = {
        "turn": turn,
        "pid": process.pid,
        "process_group": process.pid,
        "proc_start_time": proc_start_time,
        "status": "starting",
        "prompt": prompt,
        "log_path": str(log_path),
        "stderr_path": str(stderr_path),
        "started_at": _now(),
        "finished_at": None,
    }
    record.setdefault("runs", []).append(run)
    record["status"] = "starting"
    record["updated_at"] = _now()
    _atomic_json(_session_path(session_id), record)

    # thread.started is the first JSONL event. A short bounded wait lets Atlas
    # receive the resumable Codex thread id without making the turn synchronous.
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        events = _events(log_path)
        thread_id = _thread_id(events)
        if thread_id:
            if expected_thread_id is not None and thread_id != expected_thread_id:
                _terminate_process_group(
                    pid=process.pid,
                    process_group=process.pid,
                    expected_start_time=proc_start_time,
                )
                run["status"] = "failed"
                run["finished_at"] = _now()
                run["last_event_type"] = "thread.started"
                run["resume_thread_mismatch"] = {
                    "expected": expected_thread_id,
                    "observed": thread_id,
                }
                record["status"] = "failed"
                record["updated_at"] = _now()
                _atomic_json(_session_path(session_id), record)
                raise RuntimeError(
                    "Codex resume returned a different thread id; refusing an implicit new session"
                )
            record["codex_thread_id"] = thread_id
            record["status"] = (
                "running" if _alive(process.pid, proc_start_time) else _event_status(events, False)
            )
            _atomic_json(_session_path(session_id), record)
            break
        if not _alive(process.pid, proc_start_time):
            break
        time.sleep(0.05)
    return _refresh(record)


def _public(record: dict[str, Any]) -> dict[str, Any]:
    runs = record.get("runs") if isinstance(record.get("runs"), list) else []
    latest = runs[-1] if runs else None
    return {
        "session_id": record.get("session_id"),
        "codex_thread_id": record.get("codex_thread_id"),
        "repo": record.get("repo"),
        "label": record.get("label"),
        "mode": record.get("mode"),
        "status": record.get("status"),
        "turn": latest.get("turn") if isinstance(latest, dict) else 0,
        "pid": latest.get("pid") if isinstance(latest, dict) else None,
        "started_at": latest.get("started_at") if isinstance(latest, dict) else None,
        "finished_at": latest.get("finished_at") if isinstance(latest, dict) else None,
        "last_event_type": latest.get("last_event_type") if isinstance(latest, dict) else None,
        "result": latest.get("result") if isinstance(latest, dict) else None,
        "updated_at": record.get("updated_at"),
    }


def start_session(arguments: dict[str, Any]) -> dict[str, Any]:
    repo = _validate_repo(arguments.get("repo"))
    prompt = str(arguments.get("prompt") or "")
    session_id = str(uuid4())
    record = {
        "session_id": session_id,
        "codex_thread_id": None,
        "repo": str(repo),
        "label": str(arguments.get("label") or "Coding task")[:200],
        "mode": str(arguments.get("mode") or "implement")[:40],
        "status": "created",
        "created_at": _now(),
        "updated_at": _now(),
        "runs": [],
    }
    _atomic_json(_session_path(session_id), record)
    with _locked(session_id):
        return _public(_launch(record, prompt, resume=False))


def send_turn(arguments: dict[str, Any]) -> dict[str, Any]:
    session_id = str(arguments.get("session_id") or "")
    prompt = str(arguments.get("prompt") or "")
    with _locked(session_id):
        record = _refresh(_load(session_id))
        if record.get("status") in {"running", "starting"}:
            raise ValueError("The coding session already has an active Codex turn")
        return _public(_launch(record, prompt, resume=True))


def resume_session(arguments: dict[str, Any]) -> dict[str, Any]:
    session_id = str(arguments.get("session_id") or "")
    prompt = str(arguments.get("prompt") or "").strip()
    if not prompt:
        prompt = (
            "Resume the current task from the repository state as it exists now. "
            "First reconcile what is already complete, then continue only the unfinished work."
        )
    return send_turn({"session_id": session_id, "prompt": prompt})


def get_status(arguments: dict[str, Any]) -> dict[str, Any]:
    session_id = str(arguments.get("session_id") or "")
    with _locked(session_id):
        record = _refresh(_load(session_id))
        _atomic_json(_session_path(session_id), record)
        return _public(record)


def get_result(arguments: dict[str, Any]) -> dict[str, Any]:
    session_id = str(arguments.get("session_id") or "")
    with _locked(session_id):
        record = _refresh(_load(session_id))
        _atomic_json(_session_path(session_id), record)
        runs = record.get("runs") or []
        latest = runs[-1] if runs else {}
        events = _events(Path(str(latest.get("log_path") or "")))
        terminal = record.get("status") in {"completed", "failed", "interrupted", "cancelled"}
        return {
            **_public(record),
            "terminal": terminal,
            "result": _last_agent_text(events),
            "last_events": events[-20:],
        }


def cancel_session(arguments: dict[str, Any]) -> dict[str, Any]:
    session_id = str(arguments.get("session_id") or "")
    with _locked(session_id):
        record = _refresh(_load(session_id))
        runs = record.get("runs") or []
        latest = runs[-1] if runs else None
        running = bool(latest) and _alive(
            latest.get("pid"), latest.get("proc_start_time")
        )
        if running and latest:
            pid = int(latest["pid"])
            process_group = int(latest.get("process_group") or pid)
            _terminate_process_group(
                pid=pid,
                process_group=process_group,
                expected_start_time=latest.get("proc_start_time"),
            )
            latest["status"] = "cancelled"
            latest["finished_at"] = _now()
            record["status"] = "cancelled"
        elif record.get("status") in {"running", "starting", "created", "interrupted"}:
            # The process has already disappeared, but cancellation still closes
            # the non-terminal session without rewriting a completed result.
            record["status"] = "cancelled"
            if latest:
                latest["status"] = "cancelled"
                latest["finished_at"] = latest.get("finished_at") or _now()
        record["updated_at"] = _now()
        _atomic_json(_session_path(session_id), record)
        return _public(record)


TOOLS: dict[str, dict[str, Any]] = {
    "start_session": {
        "description": "Start a fresh Codex CLI coding/review session. Atlas has already resolved owner authority before this call.",
        "inputSchema": {"type": "object", "properties": {
            "repo": {"type": "string"}, "prompt": {"type": "string"},
            "label": {"type": "string"}, "mode": {"type": "string"},
        }, "required": ["repo", "prompt"], "additionalProperties": False},
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
    },
    "send_turn": {
        "description": "Send the next instruction into an existing Codex thread after its current turn has ended.",
        "inputSchema": {"type": "object", "properties": {
            "session_id": {"type": "string"}, "prompt": {"type": "string"},
        }, "required": ["session_id", "prompt"], "additionalProperties": False},
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
    },
    "get_status": {
        "description": "Read current process and Codex thread status for a coding session.",
        "inputSchema": {"type": "object", "properties": {"session_id": {"type": "string"}},
            "required": ["session_id"], "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    "get_result": {
        "description": "Read the latest structured Codex events and final agent message for a coding session.",
        "inputSchema": {"type": "object", "properties": {"session_id": {"type": "string"}},
            "required": ["session_id"], "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    "resume_session": {
        "description": "Resume an interrupted/completed Codex thread with an optional continuation instruction.",
        "inputSchema": {"type": "object", "properties": {
            "session_id": {"type": "string"}, "prompt": {"type": "string"},
        }, "required": ["session_id"], "additionalProperties": False},
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
    },
    "cancel_session": {
        "description": "Terminate the currently running Codex process for a coding session.",
        "inputSchema": {"type": "object", "properties": {"session_id": {"type": "string"}},
            "required": ["session_id"], "additionalProperties": False},
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
    },
}

HANDLERS = {
    "start_session": start_session,
    "send_turn": send_turn,
    "get_status": get_status,
    "get_result": get_result,
    "resume_session": resume_session,
    "cancel_session": cancel_session,
}


def _handle(payload: dict[str, Any]) -> None:
    request_id = payload.get("id")
    method = payload.get("method")
    if method == "initialize":
        _reply(request_id, result={
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "atlas-coding-mcp", "version": SERVER_VERSION},
        })
        return
    if method == "notifications/initialized":
        return
    if method == "tools/list":
        _reply(request_id, result={"tools": [{"name": name, **definition} for name, definition in TOOLS.items()]})
        return
    if method == "tools/call":
        params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
        name = str(params.get("name") or "")
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        handler = HANDLERS.get(name)
        if handler is None:
            _reply(request_id, result=_tool_result({"error": f"Unknown tool: {name}"}, error=True))
            return
        try:
            result = handler(arguments)
        except (OSError, RuntimeError, TypeError, ValueError, subprocess.SubprocessError) as exc:
            _reply(request_id, result=_tool_result({"error": str(exc), "type": type(exc).__name__}, error=True))
            return
        _reply(request_id, result=_tool_result(result))
        return
    if request_id is not None:
        _reply(request_id, error={"code": -32601, "message": f"Method not found: {method}"})


def main() -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    for line in sys.stdin:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            _handle(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
