"""Owner-editable host policy and governed-server configuration from Control.

The runtime has no privilege, so it never writes the effective files. It validates what the
owner typed with the same renderer the installer uses, stages the text under the owner-managed
control directory, and writes an apply request. A root-owned path unit
(deployment/systemd/atlas-host-policy.path) applies staged files and reports back through a
result file this module reads. The write path exists only behind the owner-authenticated
Control API; it is never a capability the model can call.
"""
from __future__ import annotations

import importlib.util
import json
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from atlas.config import Settings

from .connections import _atomic_write, _ensure_private_dir

STAGED_POLICY = "host-policy.toml"
STAGED_SERVERS = "mcp-servers.toml"
REQUEST = "apply.request"
RESULT = "apply.result.json"
REPO_RENDERER = Path(__file__).resolve().parents[3] / "deployment" / "host-mcp" / "atlas_host_policy.py"
MAX_TEXT = 256 * 1024


def staging_dir(settings: Settings) -> Path:
    return settings.state_dir / "control" / "host"


def load_renderer(settings: Settings):
    path = settings.host_policy_renderer if settings.host_policy_renderer.is_file() else REPO_RENDERER
    spec = importlib.util.spec_from_file_location("atlas_host_policy_renderer", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text()
    except OSError:
        return None


class HostPolicyControl:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.renderer = load_renderer(settings)

    def validate_policy(self, text: str) -> dict[str, Any]:
        if len(text.encode()) > MAX_TEXT:
            return {"ok": False, "error": "policy is too large", "notes": [], "rule": None}
        try:
            policy = self.renderer.load_policy(text)
        except (self.renderer.PolicyError, tomllib.TOMLDecodeError, ValueError) as exc:
            return {"ok": False, "error": str(exc), "notes": [], "rule": None}
        return {
            "ok": True, "error": None, "notes": list(policy["warnings"]), "rule": self.renderer.render_polkit_rules(policy),
            "units": {unit: verbs for unit, verbs in policy["units"].items()}, "grants": policy["grants"],
            "groups": policy["groups"], "systemd_tools": policy["systemd_tools"], "shell_commands": policy["shell_commands"],
            "shell_env": self.renderer.render_shell_env(policy), "systemd_env": self.renderer.render_systemd_env(policy),
        }

    def validate_servers(self, text: str) -> dict[str, Any]:
        from atlas.integrations.mcp_servers import parse_mcp_servers

        if len(text.encode()) > MAX_TEXT:
            return {"ok": False, "error": "configuration is too large", "servers": []}
        try:
            servers = parse_mcp_servers(text)
        except (TypeError, ValueError, tomllib.TOMLDecodeError) as exc:
            return {"ok": False, "error": str(exc), "servers": []}
        return {"ok": True, "error": None, "servers": [
            {"id": server.id, "family": server.family, "transport": server.transport_label, "tools": sorted(server.tools) if server.tools else None,
                "configured": server.configured, "policies": sorted(server.tool_policies)} for server in servers]}

    def status(self) -> dict[str, Any]:
        staging = staging_dir(self.settings)
        policy_text = _read_text(self.settings.host_policy_file)
        servers_text = _read_text(self.settings.mcp_servers_file) if self.settings.mcp_servers_file else None
        request = self._read_json(staging / REQUEST)
        result = self._read_json(staging / RESULT)
        return {
            "policy": {"path": str(self.settings.host_policy_file), "text": policy_text, "exists": policy_text is not None,
                "effective": self.validate_policy(policy_text) if policy_text is not None else None},
            "servers": {"path": str(self.settings.mcp_servers_file) if self.settings.mcp_servers_file else None, "text": servers_text,
                "exists": servers_text is not None, "effective": self.validate_servers(servers_text) if servers_text is not None else None},
            "pending": request, "last_result": result, "staging_dir": str(staging),
        }

    def stage(self, *, policy_text: str | None = None, servers_text: str | None = None) -> dict[str, Any]:
        """Validate, then stage the files and the apply request. Invalid text is never staged."""
        kinds: list[str] = []
        if policy_text is not None:
            verdict = self.validate_policy(policy_text)
            if not verdict["ok"]:
                raise ValueError(f"host policy rejected: {verdict['error']}")
            kinds.append("policy")
        if servers_text is not None:
            verdict = self.validate_servers(servers_text)
            if not verdict["ok"]:
                raise ValueError(f"MCP server configuration rejected: {verdict['error']}")
            kinds.append("servers")
        if not kinds:
            raise ValueError("nothing to apply")
        staging = staging_dir(self.settings)
        _ensure_private_dir(staging)
        if policy_text is not None:
            _atomic_write(staging / STAGED_POLICY, policy_text.encode(), mode=0o600)
        if servers_text is not None:
            _atomic_write(staging / STAGED_SERVERS, servers_text.encode(), mode=0o600)
        request = {"requested_at": datetime.now(UTC).isoformat(), "kinds": kinds}
        _atomic_write(staging / REQUEST, json.dumps(request, sort_keys=True).encode(), mode=0o600)
        return request

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any] | None:
        text = _read_text(path)
        if text is None:
            return None
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return {"error": "unreadable result file"}
        return payload if isinstance(payload, dict) else None
