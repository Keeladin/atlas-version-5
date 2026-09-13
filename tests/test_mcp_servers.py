"""Governed MCP servers: configuration, authority mapping, and execution through the runtime."""
from pathlib import Path

import pytest
from atlas.actions.authority import AuthorityStore
from atlas.artifacts.store import ArtifactStore
from atlas.capabilities import (
    AuthorityMode,
    CapabilityFailure,
    CapabilityRuntime,
    EffectKind,
)
from atlas.capabilities.factory import build_capability_runtime
from atlas.config import Settings
from atlas.integrations.mcp_servers import (
    GovernedMCPServer,
    MCPServerConfig,
    descriptor_for_tool,
    parse_mcp_servers,
    register_mcp_servers,
)
from atlas.integrations.mcp_stdio import (
    MCPToolError,
    MCPTransportError,
    MCPUnavailableError,
)
from atlas.persistence.models import ActionRow, OwnerAttentionRow
from atlas.registry.service import build_phase0_registry
from atlas.runtime.execution import RunExecutor
from atlas.transcript.repository import TranscriptRepository
from sqlalchemy import select
from support_mcp import FakeMCPSocketServer, socket_path, unit_tools

EXAMPLE = Path(__file__).resolve().parents[1] / "deployment" / "mcp-servers.example.toml"


def _config(path: Path, *, tools=None, policies=None) -> MCPServerConfig:
    return MCPServerConfig(id="host.systemd", family="Host services", description="Units.", transport="socket", path=path,
        tools=frozenset(tools) if tools else None, tool_policies=policies or {})


def _toml(path: Path, extra: str = "") -> str:
    return f'''
[[servers]]
id = "host.systemd"
family = "Host services"
description = "Manage the units the owner listed."
transport = "socket"
path = "{path}"
tools = ["change_unit_state", "list_loaded_units", "list_log"]

[servers.tool.change_unit_state]
effect = "execute"
authority = "auto"
authority_rules = [{{ when = {{ action = ["stop"] }}, authority = "approval_required" }}]

[servers.tool.list_log]
effect = "read"
authority = "auto"
{extra}
'''


def test_example_configuration_parses_and_pins_the_envelope_shape() -> None:
    servers = parse_mcp_servers(EXAMPLE.read_text())
    ids = {server.id for server in servers}
    assert {"host.systemd", "host.shell"} <= ids
    systemd = next(server for server in servers if server.id == "host.systemd")
    assert systemd.transport == "socket" and "get_file" not in (systemd.tools or set())
    policy = systemd.tool_policies["change_unit_state"]
    assert policy.authority == AuthorityMode.AUTO and policy.rules[0].authority == AuthorityMode.APPROVAL_REQUIRED


@pytest.mark.parametrize("bad,message", [
    ('[[servers]]\nid = "Host"\ntransport = "socket"\npath = "/x"\n', "id must look like"),
    ('[[servers]]\nid = "atlas.host"\ntransport = "socket"\npath = "/x"\n', "reserved"),
    ('[[servers]]\nid = "host.a"\ntransport = "socket"\n', "needs 'path'"),
    ('[[servers]]\nid = "host.a"\ntransport = "stdio"\n', "needs 'command'"),
    ('[[servers]]\nid = "host.a"\ntransport = "socket"\npath = "/x"\n[servers.defaults]\ndestructive_authority = "maybe"\n', "authority must be"),
    ('[[servers]]\nid = "host.a"\ntransport = "socket"\npath = "/x"\n[[servers]]\nid = "host.a"\ntransport = "socket"\npath = "/y"\n', "duplicate"),
    ('[[servers]]\nid = "host.a"\ntransport = "socket"\npath = "/x"\n[servers.tool.t]\nauthority_rules = [{ authority = "auto" }]\n', "non-empty 'when'"),
])
def test_configuration_rejections(bad, message) -> None:
    with pytest.raises(ValueError, match=message):
        parse_mcp_servers(bad)


def test_annotations_set_defaults_and_configuration_wins(tmp_path) -> None:
    server = _config(tmp_path / "s.sock")
    tools = unit_tools()
    read = descriptor_for_tool(server, tools["list_loaded_units"][0])
    assert read.effect == EffectKind.READ and read.authority == AuthorityMode.AUTO and read.id == "host.systemd.list_loaded_units"
    unannotated = descriptor_for_tool(server, tools["change_unit_state"][0])
    assert unannotated.effect == EffectKind.EXECUTE and unannotated.authority == AuthorityMode.APPROVAL_REQUIRED
    non_destructive = descriptor_for_tool(server, {"name": "ping", "annotations": {"destructiveHint": False}})
    assert non_destructive.effect == EffectKind.EXECUTE and non_destructive.authority == AuthorityMode.AUTO
    configured = parse_mcp_servers(_toml(tmp_path / "s.sock"))[0]
    overridden = descriptor_for_tool(configured, tools["change_unit_state"][0])
    assert overridden.authority == AuthorityMode.AUTO and "Authority depends on the arguments" in overridden.description
    assert descriptor_for_tool(configured, tools["get_file"][0]) is None  # not in the allowlist
    assert descriptor_for_tool(configured, tools["list_log"][0]).effect == EffectKind.READ
    assert descriptor_for_tool(server, {"name": "bad name!"}) is None
    assert descriptor_for_tool(server, {"name": "x", "inputSchema": "nope"}).input_schema == {"type": "object", "properties": {}}


def test_rule_resolver_follows_owner_policy() -> None:
    configured = parse_mcp_servers(_toml(Path("/tmp/x.sock")))[0]
    server = GovernedMCPServer(configured, client=object())  # type: ignore[arg-type]
    descriptor = descriptor_for_tool(configured, unit_tools()["change_unit_state"][0])
    resolve = server.authority_resolver(descriptor)
    assert resolve({"name": "a", "action": "stop"}) == AuthorityMode.APPROVAL_REQUIRED
    assert resolve({"name": "a", "action": "restart"}) == AuthorityMode.AUTO
    assert server.authority_resolver(descriptor_for_tool(configured, unit_tools()["list_log"][0])) is None


def test_governed_call_maps_failures_to_definite_or_ambiguous(tmp_path) -> None:
    class Client:
        def __init__(self, exc):
            self.exc = exc

        def call_tool(self, name, arguments):
            if self.exc:
                raise self.exc
            return {"name": name, **arguments}

    config = _config(tmp_path / "s.sock")
    assert GovernedMCPServer(config, client=Client(None)).call("host.systemd.list_log", {"unit": "u"}) == {"name": "list_log", "unit": "u"}
    with pytest.raises(CapabilityFailure) as completed:
        GovernedMCPServer(config, client=Client(MCPToolError("denied"))).call("host.systemd.change_unit_state", {})
    assert completed.value.phase == "completed" and completed.value.output["error"] == "denied"
    with pytest.raises(CapabilityFailure) as refused:
        GovernedMCPServer(config, client=Client(MCPUnavailableError("no socket"))).call("host.systemd.change_unit_state", {})
    assert refused.value.phase == "before_dispatch"
    with pytest.raises(RuntimeError, match="gone"):
        GovernedMCPServer(config, client=Client(MCPTransportError("gone"))).call("host.systemd.change_unit_state", {})
    with pytest.raises(ValueError):
        GovernedMCPServer(config, client=Client(None)).call("other.tool", {})


def test_register_mcp_servers_adds_capabilities_and_reports_status(tmp_path) -> None:
    live = socket_path(tmp_path, "live.sock")
    config_file = tmp_path / "mcp-servers.toml"
    config_file.write_text(_toml(live) + f'''
[[servers]]
id = "host.shell"
family = "Host diagnostics"
transport = "socket"
path = "{tmp_path / "missing.sock"}"
''')
    settings = Settings(mcp_servers_file=config_file)
    with FakeMCPSocketServer(live, unit_tools()):
        registry = build_phase0_registry(settings)
        runtime = CapabilityRuntime(registry.operations())
        statuses = register_mcp_servers(settings, registry, runtime)
    by_id = {status["id"]: status for status in statuses}
    assert by_id["host.systemd"]["configured"] is True and by_id["host.systemd"]["availability"] == "available"
    assert by_id["host.systemd"]["operations"] == ["host.systemd.change_unit_state", "host.systemd.list_loaded_units", "host.systemd.list_log"]
    assert by_id["host.shell"]["configured"] is False and by_id["host.shell"]["operations"] == []
    entries = {entry.id: entry for entry in registry.all_entries()}
    assert entries["host.systemd"].source.value == "mcp" and entries["host.systemd"].enabled is True
    assert entries["host.shell"].availability.value == "unavailable" and entries["host.shell"].enabled is False
    assert runtime.descriptor("host.systemd.get_file") is None
    assert runtime.descriptor("host.systemd.change_unit_state").authority == AuthorityMode.AUTO


def test_register_mcp_servers_reports_a_broken_configuration_without_raising(tmp_path) -> None:
    broken = tmp_path / "bad.toml"
    broken.write_text('[[servers]]\nid = "nope!"\n')
    settings = Settings(mcp_servers_file=broken)
    registry = build_phase0_registry(settings)
    statuses = register_mcp_servers(settings, registry, CapabilityRuntime())
    assert statuses[0]["error"] and statuses[0]["configured"] is False


async def _run(factory):
    async with factory() as session:
        transcript = await TranscriptRepository(session).get_or_create_active()
        run_id = await AuthorityStore(session).create_run(transcript_id=transcript.id, intent="host fixture")
        await session.commit()
        return transcript.id, run_id


@pytest.mark.asyncio
async def test_executor_governs_host_tools_with_ledger_and_approval(pg_factory, tmp_path) -> None:
    live = socket_path(tmp_path, "sd.sock")
    config_file = tmp_path / "mcp-servers.toml"
    config_file.write_text(_toml(live))
    settings = Settings(mcp_servers_file=config_file, artifact_dir=tmp_path / "artifacts")
    with FakeMCPSocketServer(live, unit_tools()) as server:
        registry = build_phase0_registry(settings)
        runtime = build_capability_runtime(settings, registry)
        runtime.policy_reader = None  # every capability enabled for this fixture
        transcript_id, run_id = await _run(pg_factory)
        executor = RunExecutor(pg_factory, runtime, ArtifactStore(settings.artifact_dir), run_id=run_id, transcript_id=transcript_id)

        restarted = await executor.tool_handler("atlas_capability_call", {"operation_id": "host.systemd.change_unit_state",
            "arguments": {"name": "empire-control.service", "action": "restart"}})
        assert restarted["status"] == "succeeded" and restarted["action_status"] == "succeeded"
        assert server.calls[-1] == ("change_unit_state", {"name": "empire-control.service", "action": "restart"})

        gated = await executor.tool_handler("atlas_capability_call", {"operation_id": "host.systemd.change_unit_state",
            "arguments": {"name": "empire-control.service", "action": "stop"}})
        assert gated["status"] == "approval_required"
        assert server.calls[-1][1]["action"] == "restart"  # stop never reached the server

        failed = await executor.tool_handler("atlas_capability_call", {"operation_id": "host.systemd.change_unit_state",
            "arguments": {"name": "ssh.service", "action": "restart"}})
        assert failed["status"] == "failed" and failed["action_status"] == "failed"
        assert "not found" in failed["message"]
        rejected = await executor.tool_handler("atlas_capability_call", {"operation_id": "host.systemd.change_unit_state",
            "arguments": {"name": "desktop-commander.service", "action": "boom"}})
        assert rejected["status"] == "failed" and rejected["output"]["failure_phase"] == "before_dispatch"

        read = await executor.tool_handler("atlas_capability_call", {"operation_id": "host.systemd.list_log",
            "arguments": {"unit": "desktop-commander.service"}})
        assert read["status"] == "succeeded"

    async with pg_factory() as session:
        actions = (await session.execute(select(ActionRow).order_by(ActionRow.created_at))).scalars().all()
        by_status = [(action.operation, action.status) for action in actions]
        assert ("host.systemd.change_unit_state", "succeeded") in by_status
        assert ("host.systemd.change_unit_state", "prepared") in by_status
        assert ("host.systemd.change_unit_state", "failed") in by_status
        assert all(action.status != "uncertain" for action in actions)
        pending = await AuthorityStore(session).pending()
        proposal = next(item for item in pending if item["state"] == "approval_required")
        assert proposal["detail"]["display_label"] == "Host: change unit state"
        assert proposal["detail"]["target"] == "stop empire-control.service"
        attention = (await session.execute(select(OwnerAttentionRow))).scalars().all()
        assert any(row.state == "approval_required" for row in attention)


@pytest.mark.asyncio
async def test_executor_marks_transport_loss_after_send_as_uncertain(pg_factory, tmp_path) -> None:
    live = socket_path(tmp_path, "sd.sock")
    config_file = tmp_path / "mcp-servers.toml"
    config_file.write_text(_toml(live))
    settings = Settings(mcp_servers_file=config_file, artifact_dir=tmp_path / "artifacts")
    with FakeMCPSocketServer(live, unit_tools()):
        registry = build_phase0_registry(settings)
        runtime = build_capability_runtime(settings, registry)
        runtime.policy_reader = None
    with FakeMCPSocketServer(live, unit_tools(), mode="close_after_call"):
        transcript_id, run_id = await _run(pg_factory)
        executor = RunExecutor(pg_factory, runtime, ArtifactStore(settings.artifact_dir), run_id=run_id, transcript_id=transcript_id)
        result = await executor.tool_handler("atlas_capability_call", {"operation_id": "host.systemd.change_unit_state",
            "arguments": {"name": "empire-control.service", "action": "restart"}})
    assert result["status"] == "failed" and result["action_status"] == "uncertain"
    async with pg_factory() as session:
        action = (await session.execute(select(ActionRow))).scalars().one()
        assert action.status == "uncertain"
