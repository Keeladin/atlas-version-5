"""Owner-configured MCP servers as governed capabilities.

Configuration (TOML, `ATLAS_MCP_SERVERS_FILE`) names each server, its transport, the tools
Atlas may see, and how effect and authority are derived. MCP annotations are hints from an
external process and only set defaults; explicit configuration always wins, and the runtime's
ledger, approval flow and capability enablement remain the safety guarantee.
"""
from __future__ import annotations

import logging
import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from atlas.capabilities import (
    AuthorityMode,
    CapabilityFailure,
    EffectKind,
    OperationDescriptor,
)
from atlas.config import Settings
from atlas.registry.models import (
    CapabilityAvailability,
    CapabilityEntry,
    CapabilitySource,
)

from .mcp_socket import MCPSocketClient
from .mcp_stdio import (
    MCPClientBase,
    MCPStdioClient,
    MCPToolError,
    MCPTransportError,
    MCPUnavailableError,
)

logger = logging.getLogger(__name__)

SERVER_ID = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")
TOOL_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
RESERVED_PREFIXES = ("atlas", "openai", "github", "google", "evidence", "memory", "storage", "schedules", "notifications")


@dataclass(frozen=True)
class AuthorityRule:
    when: dict[str, tuple[str, ...]]
    authority: AuthorityMode

    def matches(self, arguments: dict[str, Any]) -> bool:
        return all(str(arguments.get(key)) in values for key, values in self.when.items())


@dataclass(frozen=True)
class ToolPolicy:
    effect: EffectKind | None = None
    authority: AuthorityMode | None = None
    rules: tuple[AuthorityRule, ...] = ()


@dataclass(frozen=True)
class MCPServerConfig:
    id: str
    family: str
    description: str
    transport: str
    path: Path | None = None
    command: Path | None = None
    args: tuple[str, ...] = ()
    env_file: Path | None = None
    trust: str = "external"
    tools: frozenset[str] | None = None
    read_only_authority: AuthorityMode = AuthorityMode.AUTO
    destructive_authority: AuthorityMode = AuthorityMode.APPROVAL_REQUIRED
    non_destructive_authority: AuthorityMode = AuthorityMode.AUTO
    timeout: int = 45
    tool_policies: dict[str, ToolPolicy] = field(default_factory=dict)

    @property
    def configured(self) -> bool:
        if self.transport == "socket":
            return self.path is not None and self.path.is_socket()
        return self.command is not None and self.command.is_file()

    @property
    def transport_label(self) -> str:
        if self.transport == "socket":
            return f"unix socket · {self.path}"
        return f"stdio · {self.command}"


def _authority(value: Any, *, default: AuthorityMode, where: str) -> AuthorityMode:
    if value is None:
        return default
    try:
        return AuthorityMode(str(value))
    except ValueError as exc:
        raise ValueError(f"{where}: authority must be auto, approval_required or forbidden") from exc


def _effect(value: Any, *, where: str) -> EffectKind | None:
    if value is None:
        return None
    try:
        return EffectKind(str(value))
    except ValueError as exc:
        raise ValueError(f"{where}: effect must be read, create, update, delete or execute") from exc


def _tool_policy(name: str, raw: dict[str, Any], *, where: str) -> ToolPolicy:
    rules: list[AuthorityRule] = []
    for index, rule in enumerate(raw.get("authority_rules") or []):
        if not isinstance(rule, dict) or not isinstance(rule.get("when"), dict) or not rule["when"]:
            raise ValueError(f"{where}.{name}: authority_rules[{index}] needs a non-empty 'when' table")
        when = {}
        for key, values in rule["when"].items():
            if isinstance(values, (str, int, float, bool)):
                values = [values]
            if not isinstance(values, list) or not values:
                raise ValueError(f"{where}.{name}: authority_rules[{index}].when.{key} must be a value or a list")
            when[str(key)] = tuple(str(item) for item in values)
        rules.append(AuthorityRule(when=when, authority=_authority(rule.get("authority"),
            default=AuthorityMode.APPROVAL_REQUIRED, where=f"{where}.{name}.authority_rules[{index}]")))
    return ToolPolicy(effect=_effect(raw.get("effect"), where=f"{where}.{name}"),
        authority=_authority(raw.get("authority"), default=None, where=f"{where}.{name}") if raw.get("authority") is not None else None,
        rules=tuple(rules))


def parse_mcp_servers(text: str) -> list[MCPServerConfig]:
    data = tomllib.loads(text)
    servers = data.get("servers")
    if servers is None:
        return []
    if not isinstance(servers, list):
        raise TypeError("mcp servers: 'servers' must be an array of tables")
    result: list[MCPServerConfig] = []
    seen: set[str] = set()
    for index, raw in enumerate(servers):
        where = f"servers[{index}]"
        if not isinstance(raw, dict):
            raise TypeError(f"{where}: must be a table")
        server_id = str(raw.get("id") or "").strip()
        if not SERVER_ID.match(server_id):
            raise ValueError(f"{where}: id must look like host.systemd")
        if server_id.split(".", 1)[0] in RESERVED_PREFIXES:
            raise ValueError(f"{where}: id prefix '{server_id.split('.', 1)[0]}' is reserved for built-in capabilities")
        if server_id in seen:
            raise ValueError(f"{where}: duplicate id {server_id}")
        seen.add(server_id)
        transport = str(raw.get("transport") or "socket")
        if transport not in {"socket", "stdio"}:
            raise ValueError(f"{where}: transport must be socket or stdio")
        path = Path(str(raw["path"])) if raw.get("path") else None
        command = Path(str(raw["command"])) if raw.get("command") else None
        if transport == "socket" and path is None:
            raise ValueError(f"{where}: socket transport needs 'path'")
        if transport == "stdio" and command is None:
            raise ValueError(f"{where}: stdio transport needs 'command'")
        args = raw.get("args") or []
        if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
            raise ValueError(f"{where}: args must be a list of strings")
        trust = str(raw.get("trust") or "external")
        if trust not in {"internal", "external"}:
            raise ValueError(f"{where}: trust must be internal or external")
        tools_raw = raw.get("tools")
        tools: frozenset[str] | None = None
        if tools_raw is not None:
            if not isinstance(tools_raw, list) or not all(isinstance(item, str) and TOOL_NAME.match(item) for item in tools_raw):
                raise ValueError(f"{where}: tools must be a list of tool names")
            tools = frozenset(tools_raw)
        defaults = raw.get("defaults") or {}
        if not isinstance(defaults, dict):
            raise TypeError(f"{where}: defaults must be a table")
        policies_raw = raw.get("tool") or raw.get("tools_policy") or {}
        if not isinstance(policies_raw, dict):
            raise TypeError(f"{where}: tool policies must be a table keyed by tool name")
        policies = {str(name): _tool_policy(str(name), policy, where=f"{where}.tool")
            for name, policy in policies_raw.items() if isinstance(policy, dict)}
        timeout = int(raw.get("timeout_seconds") or 45)
        result.append(MCPServerConfig(
            id=server_id, family=str(raw.get("family") or server_id), description=str(raw.get("description") or f"MCP server {server_id}."),
            transport=transport, path=path, command=command, args=tuple(args),
            env_file=Path(str(raw["env_file"])) if raw.get("env_file") else None, trust=trust, tools=tools,
            read_only_authority=_authority(defaults.get("read_only_authority"), default=AuthorityMode.AUTO, where=f"{where}.defaults"),
            destructive_authority=_authority(defaults.get("destructive_authority"), default=AuthorityMode.APPROVAL_REQUIRED, where=f"{where}.defaults"),
            non_destructive_authority=_authority(defaults.get("non_destructive_authority"), default=AuthorityMode.AUTO, where=f"{where}.defaults"),
            timeout=max(5, min(timeout, 600)), tool_policies=policies,
        ))
    return result


def load_mcp_servers(path: Path | None) -> list[MCPServerConfig]:
    if path is None or not path.is_file():
        return []
    return parse_mcp_servers(path.read_text())


def descriptor_for_tool(server: MCPServerConfig, tool: dict[str, Any]) -> OperationDescriptor | None:
    name = tool.get("name")
    if not isinstance(name, str) or not TOOL_NAME.match(name):
        return None
    if server.tools is not None and name not in server.tools:
        return None
    schema = tool.get("inputSchema")
    if not isinstance(schema, dict):
        schema = {"type": "object", "properties": {}}
    annotations = tool.get("annotations") if isinstance(tool.get("annotations"), dict) else {}
    read_only = annotations.get("readOnlyHint") is True
    # Per the MCP spec an absent destructiveHint means destructive.
    destructive = annotations.get("destructiveHint") is not False
    policy = server.tool_policies.get(name, ToolPolicy())
    if read_only:
        effect, authority = EffectKind.READ, server.read_only_authority
    else:
        effect = EffectKind.EXECUTE
        authority = server.destructive_authority if destructive else server.non_destructive_authority
    if policy.effect is not None:
        effect = policy.effect
    if policy.authority is not None:
        authority = policy.authority
    description = str(tool.get("description") or f"{server.family} tool {name}.")
    if policy.rules:
        description = f"{description} Authority depends on the arguments (owner policy)."
    return OperationDescriptor(id=f"{server.id}.{name}", capability_id=server.id, family=server.family,
        description=description, input_schema=schema, effect=effect, authority=authority, trust=server.trust)


def rule_resolver(policy: ToolPolicy, static_authority: AuthorityMode) -> Callable[[dict[str, Any]], AuthorityMode] | None:
    if not policy.rules:
        return None

    def resolve(arguments: dict[str, Any]) -> AuthorityMode:
        for rule in policy.rules:
            if rule.matches(arguments if isinstance(arguments, dict) else {}):
                return rule.authority
        return static_authority

    return resolve


def _read_env_file(path: Path | None) -> dict[str, str]:
    if path is None or not path.is_file():
        return {}
    environment: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        environment[key.strip()] = value.strip().strip('"')
    return environment


class GovernedMCPServer:
    def __init__(self, config: MCPServerConfig, client: MCPClientBase | None = None) -> None:
        self.config = config
        if client is not None:
            self.client = client
        elif config.transport == "socket":
            self.client = MCPSocketClient(config.path, timeout=config.timeout)  # type: ignore[arg-type]
        else:
            self.client = MCPStdioClient(config.command, list(config.args),  # type: ignore[arg-type]
                environment=_read_env_file(config.env_file), timeout=config.timeout)

    def discover_operations(self) -> list[OperationDescriptor]:
        descriptors = [descriptor_for_tool(self.config, tool) for tool in self.client.list_tools()]
        return sorted((item for item in descriptors if item is not None), key=lambda item: item.id)

    def tool_name(self, operation_id: str) -> str:
        prefix = f"{self.config.id}."
        if not operation_id.startswith(prefix):
            raise ValueError(f"Not an operation of {self.config.id}")
        return operation_id[len(prefix):]

    def call(self, operation_id: str, arguments: dict[str, Any]) -> Any:
        name = self.tool_name(operation_id)
        try:
            return self.client.call_tool(name, arguments)
        except MCPToolError as exc:
            raise CapabilityFailure(str(exc), phase="completed", output={"tool": name, "error": str(exc)}) from exc
        except MCPUnavailableError as exc:
            raise CapabilityFailure(str(exc), phase="before_dispatch", output={"tool": name}) from exc
        except MCPTransportError as exc:
            raise RuntimeError(str(exc)) from exc

    def authority_resolver(self, descriptor: OperationDescriptor):
        policy = self.config.tool_policies.get(self.tool_name(descriptor.id), ToolPolicy())
        return rule_resolver(policy, descriptor.authority)


def register_mcp_servers(settings: Settings, registry, runtime) -> list[dict[str, Any]]:
    """Add every configured server as a capability; discovery failures leave it visibly unavailable."""
    try:
        configs = load_mcp_servers(settings.mcp_servers_file)
    except (OSError, TypeError, ValueError, tomllib.TOMLDecodeError) as exc:
        logger.error("MCP server configuration rejected: %s", exc)
        return [{"id": "mcp.servers", "label": "MCP servers", "configured": False, "availability": "unavailable",
            "operations": [], "transport": str(settings.mcp_servers_file), "error": str(exc)}]
    statuses: list[dict[str, Any]] = []
    for config in configs:
        configured = config.configured
        entry = CapabilityEntry(id=config.id, family=config.family, description=config.description,
            source=CapabilitySource.MCP, enabled=configured,
            availability=CapabilityAvailability.AVAILABLE if configured else CapabilityAvailability.UNAVAILABLE,
            executable_operations=[], trust=config.trust)  # type: ignore[arg-type]
        registry.upsert(entry)
        error: str | None = None
        operations: list[str] = []
        if configured:
            server = GovernedMCPServer(config)
            try:
                descriptors = server.discover_operations()
            except (RuntimeError, TypeError, OSError, ValueError) as exc:
                error = f"{type(exc).__name__}: {exc}"
                descriptors = []
                entry.availability = CapabilityAvailability.UNAVAILABLE
                entry.enabled = False
            for descriptor in descriptors:
                registry.register_operation(descriptor)
                runtime.register(descriptor, lambda arguments, operation_id=descriptor.id, server=server: server.call(operation_id, arguments),
                    authority_resolver=server.authority_resolver(descriptor))
                operations.append(descriptor.id)
        statuses.append({"id": config.id, "label": config.family, "configured": configured,
            "availability": entry.availability.value, "operations": operations,
            "transport": config.transport_label, "error": error})
        if error:
            logger.warning("MCP server %s is unavailable: %s", config.id, error)
    return statuses
