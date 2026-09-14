import pytest
from atlas.integrations.mcp_socket import MCPSocketClient
from atlas.integrations.mcp_stdio import (
    MCPToolError,
    MCPTransportError,
    MCPUnavailableError,
)
from support_mcp import FakeMCPSocketServer, socket_path, unit_tools


def test_socket_client_speaks_the_stdio_exchange_one_connection_per_request(tmp_path) -> None:
    path = socket_path(tmp_path)
    with FakeMCPSocketServer(path, unit_tools()) as server:
        client = MCPSocketClient(path, timeout=5)
        tools = client.list_tools()
        assert {tool["name"] for tool in tools} == {"change_unit_state", "list_loaded_units", "list_log", "get_file"}
        output = client.call_tool("change_unit_state", {"name": "empire-control.service", "action": "restart"})
        assert output == {"name": "empire-control.service", "state": "active", "job": "done"}
        assert client.call_tool("list_log", {"unit": "desktop-commander.service"}).startswith("Sep 13")
        assert server.calls[0] == ("change_unit_state", {"name": "empire-control.service", "action": "restart"})
        assert server.connections == 3  # list, call, call: one process-equivalent per request


def test_socket_client_distinguishes_tool_failure_from_transport_failure(tmp_path) -> None:
    path = socket_path(tmp_path)
    with FakeMCPSocketServer(path, unit_tools()):
        client = MCPSocketClient(path, timeout=5)
        with pytest.raises(MCPToolError, match="Interactive authentication"):
            client.call_tool("change_unit_state", {"name": "desktop-commander.service", "action": "boom"})
        with pytest.raises(RuntimeError, match="Unknown tool"):
            client.call_tool("nope", {})
    with FakeMCPSocketServer(path, unit_tools(), mode="close_after_call"), pytest.raises(MCPTransportError, match="closed"):
        MCPSocketClient(path, timeout=5).call_tool("list_log", {})
    with FakeMCPSocketServer(path, unit_tools(), mode="hang_after_call"), pytest.raises(MCPTransportError, match="timed out"):
        MCPSocketClient(path, timeout=1).call_tool("list_log", {})


def test_socket_client_reports_unreachable_before_any_request(tmp_path) -> None:
    with pytest.raises(MCPUnavailableError):
        MCPSocketClient(tmp_path / "missing.sock", timeout=1).list_tools()
