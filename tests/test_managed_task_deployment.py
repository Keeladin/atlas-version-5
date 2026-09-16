from pathlib import Path

from atlas.integrations.mcp_servers import parse_mcp_servers

ROOT = Path(__file__).resolve().parents[1]
DEPLOYMENT = ROOT / "deployment"
SYSTEMD = DEPLOYMENT / "systemd"


def test_managed_mcp_config_uses_central_authority_and_coding_socket():
    text = (DEPLOYMENT / "mcp-servers.example.toml").read_text()
    servers = {item.id: item for item in parse_mcp_servers(text)}

    coding = servers["coding.agent"]
    assert coding.transport == "socket"
    assert str(coding.path) == "/run/atlas-v5/mcp/coding-agent.sock"
    assert coding.tools == frozenset({
        "start_session", "send_turn", "get_status", "get_result",
        "resume_session", "cancel_session",
    })
    tasks = servers["workspace.tasks"]
    assert tasks.transport == "stdio"
    assert tasks.tools == frozenset({"create", "list", "get", "cancel", "resume"})
    assert "authority_rules" not in text
    assert "approval_required" not in text


def test_coding_agent_runs_as_owner_behind_group_socket():
    socket = (SYSTEMD / "atlas-coding-agent.socket").read_text()
    service = (SYSTEMD / "atlas-coding-agent.service").read_text()
    assert "ListenStream=/run/atlas-v5/mcp/coding-agent.sock" in socket
    assert "SocketGroup=atlas-v5" in socket and "SocketMode=0660" in socket
    assert "Accept=no" in socket
    assert "User=jaco" in service and "Group=jaco" in service
    assert "coding_mcp_socket_server" in service
    assert "ATLAS_CODING_ROOTS=/home/jaco/Projects:/home/jaco/Workspace" in service
    assert "NoNewPrivileges=yes" in service
    assert "ProtectSystem=strict" in service
    assert "ProtectHome=read-only" in service
    assert "ReadWritePaths=/home/jaco/Projects /home/jaco/Workspace" in service
    assert "sudo" not in service


def test_managed_worker_stays_isolated_as_atlas_runtime_identity():
    service = (SYSTEMD / "atlas-v5-managed-tasks.service").read_text()
    assert "User=atlas-v5" in service and "Group=atlas-v5" in service
    assert "PartOf=atlas-v5.service" in service
    assert "atlas-coding-agent.socket" in service
    assert "atlas.runtime.managed_tasks_worker" in service
    assert "ATLAS_MANAGED_TASK_MAX_NO_PROGRESS=3" in service
    assert "ATLAS_MANAGED_TASK_MAX_TRANSIENT_FAILURES=48" in service
    assert "ReadWritePaths=/var/lib/atlas-v5" in service
    assert "BindReadOnlyPaths=/home/jaco/Projects:/var/lib/atlas-v5/projects" in service


def test_installer_provisions_and_smoke_tests_coding_worker():
    script = (DEPLOYMENT / "install-host-mcp.sh").read_text()
    assert "atlas-coding-agent.socket atlas-coding-agent.service" in script
    assert "atlas-v5-managed-tasks.service" in script
    assert "systemctl enable --now atlas-coding-agent.socket" in script
    assert "systemctl enable atlas-v5-managed-tasks.service" in script
    assert "/run/atlas-v5/mcp/coding-agent.sock" in script
    assert "CODEX_BIN=/home/jaco/.local/node/bin/codex" in script
    assert "coding.agent" in script and "workspace.tasks" in script
