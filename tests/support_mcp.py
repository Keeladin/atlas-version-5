"""A fake MCP server on a unix socket, one connection per request, mirroring systemd Accept=yes."""
from __future__ import annotations

import json
import socket
import socketserver
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, Self

ToolHandler = Callable[[dict[str, Any]], Any]


class ToolError(Exception):
    """Raised by a fake tool handler to produce an isError response."""


class FakeMCPSocketServer:
    def __init__(self, path: Path, tools: dict[str, tuple[dict[str, Any], ToolHandler]], *,
            mode: str = "normal") -> None:
        self.path = path
        self.tools = tools
        self.mode = mode  # normal | close_after_call | hang_after_call
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.connections = 0
        outer = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                outer.connections += 1
                while True:
                    line = self.rfile.readline()
                    if not line:
                        return
                    try:
                        request = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    response = outer._respond(request)
                    if response is None:
                        continue
                    if response == "close":
                        return
                    if response == "hang":
                        threading.Event().wait(5)
                        return
                    self.wfile.write((json.dumps(response) + "\n").encode())
                    self.wfile.flush()

        class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True
            allow_reuse_address = True

        self.server = Server(str(path), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def _respond(self, request: dict[str, Any]) -> Any:
        method = request.get("method")
        request_id = request.get("id")
        if method == "initialize":
            return {"jsonrpc": "2.0", "id": request_id, "result": {"protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}}, "serverInfo": {"name": "fake", "version": "0"}}}
        if method == "notifications/initialized":
            return None
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": [definition for definition, _ in self.tools.values()]}}
        if method == "tools/call":
            params = request.get("params") or {}
            name = str(params.get("name"))
            arguments = params.get("arguments") or {}
            self.calls.append((name, arguments))
            if self.mode == "close_after_call":
                return "close"
            if self.mode == "hang_after_call":
                return "hang"
            entry = self.tools.get(name)
            if entry is None:
                return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": f"Unknown tool: {name}"}}
            try:
                output = entry[1](arguments)
            except ToolError as exc:
                return {"jsonrpc": "2.0", "id": request_id, "result": {"isError": True,
                    "content": [{"type": "text", "text": str(exc)}]}}
            text = output if isinstance(output, str) else json.dumps(output)
            result: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
            if isinstance(output, dict):
                result["structuredContent"] = output
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Method not found"}}

    def __enter__(self) -> Self:
        self.thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def unit_tools() -> dict[str, tuple[dict[str, Any], ToolHandler]]:
    """Tools shaped like openSUSE/systemd-mcp, without annotations (as upstream ships them)."""
    state = {"units": {"desktop-commander.service": "active", "empire-control.service": "failed"}}

    def change_unit_state(arguments: dict[str, Any]) -> Any:
        unit, action = str(arguments.get("name")), str(arguments.get("action"))
        if unit not in state["units"]:
            raise ToolError(f"Unit {unit} not found")
        if unit == "ssh.service" or action == "boom":
            raise ToolError("Interactive authentication required.")
        state["units"][unit] = "inactive" if action == "stop" else "active"
        return {"name": unit, "state": state["units"][unit], "job": "done"}

    def list_loaded_units(arguments: dict[str, Any]) -> Any:
        return {"units": [{"name": name, "active": value} for name, value in state["units"].items()]}

    def list_log(arguments: dict[str, Any]) -> Any:
        return "Sep 13 10:00:00 ubuntuserver desktop-commander[1]: ✅ Device ready:"

    def get_file(arguments: dict[str, Any]) -> Any:
        return "secret"

    return {
        "change_unit_state": ({"name": "change_unit_state", "description": "Change the state of a unit or service",
            "inputSchema": {"type": "object", "properties": {"name": {"type": "string", "description": "Exact name of unit to change state"},
                "action": {"type": "string", "enum": ["restart", "restart_force", "start", "stop", "stop_kill", "reload", "enable", "enable_force", "disable"]}},
                "required": ["name", "action"]}}, change_unit_state),
        "list_loaded_units": ({"name": "list_loaded_units", "description": "List systemd units that are currently loaded in memory",
            "inputSchema": {"type": "object", "properties": {"pattern": {"type": "string"}}},
            "annotations": {"readOnlyHint": True}}, list_loaded_units),
        "list_log": ({"name": "list_log", "description": "Get the last log entries for the given service or unit",
            "inputSchema": {"type": "object", "properties": {"unit": {"type": "string"}, "lines": {"type": "integer"}}}}, list_log),
        "get_file": ({"name": "get_file", "description": "Read a file from the system",
            "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}}}, get_file),
    }


def socket_path(tmp_path: Path, name: str = "mcp.sock") -> Path:
    path = tmp_path / name
    if len(str(path)) > 100:
        raise RuntimeError("tmp_path too long for a unix socket")
    return path


def can_connect(path: Path) -> bool:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(str(path))
        return True
    except OSError:
        return False
    finally:
        sock.close()
