"""Minimal MCP JSON-RPC client: one server process (or connection) per request."""
import json
import os
import select
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = "2025-06-18"


class MCPToolError(RuntimeError):
    """The server ran the tool and reported a definite failure (isError)."""


class MCPTransportError(RuntimeError):
    """The transport failed after a request was sent; the effect of an in-flight call is unknown."""


class MCPUnavailableError(RuntimeError):
    """The server could not be reached before any request was sent."""


def exchange(send: Callable[[dict[str, Any]], None], receive: Callable[[int], dict[str, Any]],
        method: str, params: dict[str, Any]) -> dict[str, Any]:
    """The MCP handshake plus one request, independent of transport."""
    send({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "atlas-v5", "version": "0.1.0"},
        },
    })
    initialize = receive(1)
    if initialize.get("error"):
        raise RuntimeError(json.dumps(initialize["error"], ensure_ascii=False))
    send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
    send({"jsonrpc": "2.0", "id": 2, "method": method, "params": params})
    response = receive(2)
    if response.get("error"):
        raise RuntimeError(json.dumps(response["error"], ensure_ascii=False))
    result = response.get("result", {})
    if not isinstance(result, dict):
        raise TypeError("MCP server returned an unexpected response")
    return result


def _parse_line(line: str, expected_id: int) -> dict[str, Any] | None:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None
    if isinstance(payload, dict) and payload.get("id") == expected_id:
        return payload
    return None


class MCPClientBase:
    timeout: int = 45

    def list_tools(self) -> list[dict[str, Any]]:
        payload = self._request("tools/list", {})
        tools = payload.get("tools", []) if isinstance(payload, dict) else []
        return [tool for tool in tools if isinstance(tool, dict)]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        payload = self._request("tools/call", {"name": name, "arguments": arguments})
        if not isinstance(payload, dict):
            return payload
        if payload.get("isError") is True:
            raise MCPToolError(self._content_text(payload) or f"MCP tool {name} failed")
        structured = payload.get("structuredContent")
        if structured is not None:
            return structured
        text = self._content_text(payload)
        if text is None:
            return payload.get("content", payload)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    @staticmethod
    def _content_text(payload: dict[str, Any]) -> str | None:
        content = payload.get("content")
        if not isinstance(content, list):
            return None
        parts = [str(item.get("text")) for item in content if isinstance(item, dict) and item.get("type") == "text" and item.get("text") is not None]
        return "\n".join(parts) if parts else None


class MCPStdioClient(MCPClientBase):
    def __init__(
        self,
        command: Path,
        arguments: list[str] | None = None,
        *,
        environment: dict[str, str] | None = None,
        timeout: int = 45,
    ) -> None:
        self.command = command
        self.arguments = arguments or []
        self.environment = environment or {}
        self.timeout = timeout

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        env = os.environ.copy()
        env.update(self.environment)
        try:
            process = subprocess.Popen(
                [str(self.command), *self.arguments],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                bufsize=1,
            )
        except OSError as exc:
            raise MCPUnavailableError(f"MCP server could not be started: {exc}") from exc
        if process.stdin is None or process.stdout is None:
            process.kill()
            raise MCPUnavailableError("MCP stdio transport could not be opened")

        def send(payload: dict[str, Any]) -> None:
            process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
            process.stdin.flush()

        def receive(expected_id: int) -> dict[str, Any]:
            while True:
                ready, _, _ = select.select([process.stdout], [], [], self.timeout)
                if not ready:
                    raise MCPTransportError(f"MCP server timed out during {method}")
                line = process.stdout.readline()
                if not line:
                    raise MCPTransportError("MCP server closed the stdio transport")
                payload = _parse_line(line, expected_id)
                if payload is not None:
                    return payload

        try:
            return exchange(send, receive, method, params)
        finally:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
