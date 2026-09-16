"""Persistent socket-activated transport for the owner-side coding MCP.

The stdio coding module owns tools and session state. This transport stays alive so Codex
children may continue after the individual Atlas MCP request has returned.
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
from typing import Any, BinaryIO

from .coding_mcp_server import (
    HANDLERS,
    PROTOCOL_VERSION,
    SERVER_VERSION,
    STATE_DIR,
    TOOLS,
)


def _tool_result(value: Any, *, error: bool = False) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False, default=str)}],
        "structuredContent": value,
        "isError": error,
    }


def _response(payload: dict[str, Any]) -> dict[str, Any] | None:
    request_id = payload.get("id")
    method = payload.get("method")
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "atlas-coding-mcp", "version": SERVER_VERSION},
            },
        }
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"tools": [{"name": name, **definition} for name, definition in TOOLS.items()]},
        }
    if method == "tools/call":
        params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
        name = str(params.get("name") or "")
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        handler = HANDLERS.get(name)
        if handler is None:
            result = _tool_result({"error": f"Unknown tool: {name}"}, error=True)
        else:
            try:
                result = _tool_result(handler(arguments))
            except (OSError, TypeError, ValueError, subprocess.SubprocessError) as exc:
                result = _tool_result({"error": str(exc), "type": type(exc).__name__}, error=True)
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    if request_id is not None:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }
    return None


def _serve_connection(connection: socket.socket) -> None:
    reader: BinaryIO = connection.makefile("rb")
    writer: BinaryIO = connection.makefile("wb")
    try:
        for raw in reader:
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            response = _response(payload)
            if response is None:
                continue
            writer.write(json.dumps(response, separators=(",", ":"), default=str).encode() + b"\n")
            writer.flush()
    finally:
        reader.close()
        writer.close()
        connection.close()


def _systemd_listener() -> socket.socket:
    listen_pid = int(os.environ.get("LISTEN_PID") or 0)
    listen_fds = int(os.environ.get("LISTEN_FDS") or 0)
    if listen_pid != os.getpid() or listen_fds < 1:
        raise RuntimeError("coding MCP requires systemd socket activation")
    return socket.socket(fileno=3)


def main() -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    # The persistent MCP service intentionally does not retain Popen objects.
    # Auto-reap completed Codex children so a zombie cannot look like a running turn.
    signal.signal(signal.SIGCHLD, signal.SIG_IGN)
    listener = _systemd_listener()
    while True:
        connection, _ = listener.accept()
        _serve_connection(connection)


if __name__ == "__main__":
    raise SystemExit(main())