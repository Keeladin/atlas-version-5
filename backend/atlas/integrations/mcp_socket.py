"""MCP over a unix socket: the same one-connection-per-request exchange as stdio.

Pairs with systemd socket activation (`Accept=yes`): each connection becomes one server
process, running as whatever identity the service template declares, with stdin/stdout
bound to the socket. Closing the connection ends that process.
"""
import json
import socket
from pathlib import Path
from typing import Any

from .mcp_stdio import (
    MCPClientBase,
    MCPTransportError,
    MCPUnavailableError,
    _parse_line,
    exchange,
)


class MCPSocketClient(MCPClientBase):
    def __init__(self, path: Path, *, timeout: int = 45, connect_timeout: float = 5.0) -> None:
        self.path = path
        self.timeout = timeout
        self.connect_timeout = connect_timeout

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.connect_timeout)
        try:
            sock.connect(str(self.path))
        except OSError as exc:
            sock.close()
            raise MCPUnavailableError(f"MCP socket {self.path} is not reachable: {exc}") from exc
        sock.settimeout(self.timeout)
        reader = sock.makefile("r", encoding="utf-8", newline="\n")

        def send(payload: dict[str, Any]) -> None:
            try:
                sock.sendall((json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8"))
            except OSError as exc:
                raise MCPTransportError(f"MCP socket write failed during {method}: {exc}") from exc

        def receive(expected_id: int) -> dict[str, Any]:
            while True:
                try:
                    line = reader.readline()
                except TimeoutError as exc:
                    raise MCPTransportError(f"MCP server timed out during {method}") from exc
                except OSError as exc:
                    raise MCPTransportError(f"MCP socket read failed during {method}: {exc}") from exc
                if not line:
                    raise MCPTransportError("MCP server closed the socket transport")
                payload = _parse_line(line, expected_id)
                if payload is not None:
                    return payload

        try:
            return exchange(send, receive, method, params)
        finally:
            try:
                reader.close()
            finally:
                sock.close()
