#!/usr/bin/env python3
"""Probe a governed MCP socket: handshake, list tools, optionally call one. Standard library only.

    atlas_mcp_probe.py /run/atlas-v5/mcp/systemd.sock
    atlas_mcp_probe.py /run/atlas-v5/mcp/shell.sock shell_execute '{"command": ["id"]}'
"""
from __future__ import annotations

import json
import socket
import sys


def request(path: str, method: str, params: dict) -> dict:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(60)
    sock.connect(path)
    reader = sock.makefile("r", encoding="utf-8")

    def send(payload: dict) -> None:
        sock.sendall((json.dumps(payload) + "\n").encode())

    def receive(expected: int) -> dict:
        while True:
            line = reader.readline()
            if not line:
                raise SystemExit("server closed the connection")
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and payload.get("id") == expected:
                return payload

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18",
            "capabilities": {}, "clientInfo": {"name": "atlas-probe", "version": "0"}}})
        receive(1)
        send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        send({"jsonrpc": "2.0", "id": 2, "method": method, "params": params})
        return receive(2)
    finally:
        sock.close()


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    path = sys.argv[1]
    if len(sys.argv) >= 3:
        arguments = json.loads(sys.argv[3]) if len(sys.argv) >= 4 else {}
        response = request(path, "tools/call", {"name": sys.argv[2], "arguments": arguments})
        print(json.dumps(response.get("result", response), indent=2)[:4000])
        return 0 if not response.get("error") and not (response.get("result") or {}).get("isError") else 1
    response = request(path, "tools/list", {})
    tools = (response.get("result") or {}).get("tools") or []
    if response.get("error"):
        print(json.dumps(response["error"]))
        return 1
    for tool in tools:
        props = list(((tool.get("inputSchema") or {}).get("properties") or {}).keys())
        annotations = tool.get("annotations") or {}
        print(f"{tool.get('name')}  args={props}  annotations={annotations}")
    print(f"{len(tools)} tools")
    return 0


if __name__ == "__main__":
    sys.exit(main())
