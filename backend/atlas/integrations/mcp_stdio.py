import json
import os
import select
import subprocess
from pathlib import Path
from typing import Any


class MCPStdioClient:
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

    def list_tools(self) -> list[dict[str, Any]]:
        payload = self._request("tools/list", {})
        tools = payload.get("tools", []) if isinstance(payload, dict) else []
        return [tool for tool in tools if isinstance(tool, dict)]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        payload = self._request("tools/call", {"name": name, "arguments": arguments})
        if not isinstance(payload, dict):
            return payload
        if payload.get("isError") is True:
            raise RuntimeError(self._content_text(payload) or f"MCP tool {name} failed")
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
        env = os.environ.copy()
        env.update(self.environment)
        process = subprocess.Popen(
            [str(self.command), *self.arguments],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            bufsize=1,
        )
        if process.stdin is None or process.stdout is None:
            process.kill()
            raise RuntimeError("MCP stdio transport could not be opened")

        def send(payload: dict[str, Any]) -> None:
            process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
            process.stdin.flush()

        def receive(expected_id: int) -> dict[str, Any]:
            while True:
                ready, _, _ = select.select([process.stdout], [], [], self.timeout)
                if not ready:
                    raise RuntimeError(f"MCP server timed out during {method}")
                line = process.stdout.readline()
                if not line:
                    raise RuntimeError("MCP server closed the stdio transport")
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict) and payload.get("id") == expected_id:
                    return payload

        try:
            send({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
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
        finally:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)

    @staticmethod
    def _content_text(payload: dict[str, Any]) -> str | None:
        content = payload.get("content")
        if not isinstance(content, list):
            return None
        parts = [str(item.get("text")) for item in content if isinstance(item, dict) and item.get("type") == "text" and item.get("text") is not None]
        return "\n".join(parts) if parts else None
