"""MCP transport for the shared durable workspace task service."""
from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from atlas.workspace import tasks

PROTOCOL_VERSION = "2025-06-18"
SERVER_VERSION = "0.1.0"


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


TOOLS: dict[str, dict[str, Any]] = {
    "create": {
        "description": "Create a durable managed task only after the owner has agreed the objective, scope, acceptance criteria and execution authority. Before creating it, discover the exact operations needed and include them in authority_grants; Atlas runtime preflights those grants so approved work can continue without repeated permission prompts.",
        "inputSchema": {"type": "object", "properties": {
            "title": {"type": "string", "maxLength": 160},
            "objective": {"type": "string", "minLength": 1, "maxLength": 800},
            "scope": {"type": "array", "items": {"type": "string"}, "maxItems": 16},
            "authority_grants": {"type": "array", "items": {"type": "string", "minLength": 1}, "maxItems": 64},
            "acceptance_criteria": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 32},
            "checkpoints": {"type": "array", "items": {"type": "string"}, "maxItems": 32},
            "workspace_path": {"type": "string", "maxLength": 1000},
            "project_id": {"type": "string"}, "source_chat_id": {"type": "string"},
            "policy_preset": {"type": "string", "maxLength": 120},
            "handoff": {"type": "string", "maxLength": 4000}
        }, "required": ["objective", "acceptance_criteria"], "additionalProperties": False},
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
    },
    "list": {
        "description": "List durable managed workspace tasks and their current progress.",
        "inputSchema": {"type": "object", "properties": {
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            "include_terminal": {"type": "boolean"}
        }, "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    "get": {
        "description": "Read one managed task by task ID, including acceptance, authority grants, checkpoint and controller state.",
        "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"], "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    "cancel": {
        "description": "Cancel a managed task and stop its active Atlas/Codex execution as one lifecycle operation.",
        "inputSchema": {"type": "object", "properties": {
            "task_id": {"type": "string"}, "reason": {"type": "string", "maxLength": 500}
        }, "required": ["task_id"], "additionalProperties": False},
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
    },
    "resume": {
        "description": "Clear a managed task controller stall and make it eligible for automatic continuation again.",
        "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"], "additionalProperties": False},
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
    },
}

HANDLERS = {
    "create": tasks.create_task,
    "list": tasks.list_tasks,
    "get": tasks.get_task,
    "cancel": tasks.cancel_task,
    "resume": tasks.resume_task,
}


def _handle(payload: dict[str, Any]) -> None:
    request_id = payload.get("id")
    method = payload.get("method")
    if method == "initialize":
        _reply(request_id, result={
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "atlas-workspace-tasks-mcp", "version": SERVER_VERSION},
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
            result = asyncio.run(handler(arguments))
        except (OSError, RuntimeError, ValueError) as exc:
            _reply(request_id, result=_tool_result({"error": str(exc), "type": type(exc).__name__}, error=True))
            return
        _reply(request_id, result=_tool_result(result))
        return
    if request_id is not None:
        _reply(request_id, error={"code": -32601, "message": f"Method not found: {method}"})


def main() -> int:
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
