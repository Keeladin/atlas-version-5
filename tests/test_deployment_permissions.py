from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOYMENT = ROOT / "deployment"


def test_control_state_directory_is_provisioned_for_runtime_user() -> None:
    deploy = (DEPLOYMENT / "deploy-host.sh").read_text()
    bootstrap = (DEPLOYMENT / "bootstrap-host.sh").read_text()
    expected = "/var/lib/atlas-v5/control"
    assert expected in deploy
    assert expected in bootstrap
    assert "install -d -o atlas-v5 -g atlas-v5 -m 0700" in deploy


def test_normal_deploy_does_not_walk_owner_projects() -> None:
    script = (DEPLOYMENT / "deploy-host.sh").read_text()
    assert "find /home/jaco/Projects" not in script
    assert "reconcile-project-access.sh" in script


def test_maintenance_acl_grant_is_confined_to_explicit_read_only_roots() -> None:
    script = (DEPLOYMENT / "grant-maintenance-access.sh").read_text()
    acl_lines = [line for line in script.splitlines() if "setfacl" in line and "find" in line]
    assert acl_lines
    assert all(
        '${APP_ROOT}' in line or '${OBSERVER_ROOT}' in line for line in acl_lines
    )
    assert "APP_ROOT=/opt/atlas-v5" in script
    assert "OBSERVER_ROOT=/var/lib/atlas-v5/observer" in script
    assert "/etc/atlas-v5" not in "\n".join(acl_lines)


def test_project_reconcile_uses_explicit_acl_masks() -> None:
    script = (DEPLOYMENT / "reconcile-project-access.sh").read_text()
    assert "m::rwx" in script
    assert "d:m::rwx" in script
    assert "m::rw-" in script

def test_deploy_stops_runtime_before_migration_and_starts_after() -> None:
    script = (DEPLOYMENT / "deploy-host.sh").read_text()
    stop = script.index("systemctl stop atlas-v5.service")
    migrate = script.index("/opt/atlas-v5/venv/bin/alembic upgrade head")
    start = script.index("systemctl start atlas-v5.service")
    assert stop < migrate < start
    assert "systemctl restart atlas-v5.service" not in script


def test_deploy_enters_maintenance_before_replacing_code_or_dependencies():
    script = (DEPLOYMENT / "deploy-host.sh").read_text()
    stop = script.index("systemctl stop atlas-v5.service")
    assert stop < script.index('rsync -a --delete')
    assert stop < script.index('"${UV_BIN}" sync')
    assert stop < script.index('install -d -o root -g atlas-v5')


def test_production_owner_projects_are_mounted_read_only():
    unit = (DEPLOYMENT / 'systemd' / 'atlas-v5.service').read_text()
    assert 'BindReadOnlyPaths=/home/jaco/Projects:/var/lib/atlas-v5/projects' in unit
    assert 'BindPaths=/home/jaco/Projects:' not in unit

def test_memory_maintenance_timer_is_bounded_and_enabled_by_deploy() -> None:
    deploy = (DEPLOYMENT / "deploy-host.sh").read_text()
    service = (DEPLOYMENT / "systemd" / "atlas-v5-memory.service").read_text()
    timer = (DEPLOYMENT / "systemd" / "atlas-v5-memory.timer").read_text()
    assert "ExecStart=/opt/atlas-v5/venv/bin/python -m atlas.memory" in service
    assert "TimeoutStartSec=4min" in service
    assert "OnActiveSec=30s" in timer
    assert "OnUnitActiveSec=5min" in timer
    assert "systemctl enable atlas-v5-memory.timer" in deploy
    assert "systemctl start atlas-v5-memory.timer" in deploy


def test_deploy_quiesces_memory_worker_before_replacing_runtime() -> None:
    script = (DEPLOYMENT / "deploy-host.sh").read_text()
    stop_timer = script.index("systemctl stop atlas-v5-memory.timer")
    stop_worker = script.index("systemctl stop atlas-v5-memory.service")
    replace_code = script.index("rsync -a --delete")
    sync_dependencies = script.index('"${UV_BIN}" sync')
    assert stop_timer < replace_code
    assert stop_worker < replace_code
    assert stop_worker < sync_dependencies


def test_runtime_gets_journal_group_for_the_connector_monitor_and_push_defaults() -> None:
    deploy = (DEPLOYMENT / "deploy-host.sh").read_text()
    assert "usermod -aG systemd-journal atlas-v5" in deploy
    assert deploy.index("usermod -aG systemd-journal") < deploy.index("systemctl start atlas-v5.service")
    assert "ATLAS_RDC_MONITOR_ENABLED=true" in deploy
    assert "ATLAS_PUSH_VAPID_PRIVATE_KEY_FILE=/etc/atlas-v5/secrets/push-vapid-private-key" in deploy
    bootstrap = (DEPLOYMENT / "bootstrap-push-vapid.sh").read_text()
    assert "install -o root -g atlas-v5 -m 0640" in bootstrap
    assert "prime256v1" in bootstrap
    unit = (DEPLOYMENT / "systemd" / "atlas-v5.service").read_text()
    assert "ProtectHome=yes" in unit  # the monitor reads the journal, never the owner's home


def test_governed_mcp_servers_run_as_the_tools_identity_behind_group_sockets() -> None:
    for name in ("atlas-mcp-systemd", "atlas-mcp-shell"):
        socket = (DEPLOYMENT / "systemd" / f"{name}.socket").read_text()
        service = (DEPLOYMENT / "systemd" / f"{name}@.service").read_text()
        assert "SocketUser=root" in socket and "SocketGroup=atlas-v5" in socket and "SocketMode=0660" in socket
        assert "Accept=yes" in socket and f"ListenStream=/run/atlas-v5/mcp/{name.split('-')[-1]}.sock" in socket
        assert "User=atlas-tools" in service and "StandardInput=socket" in service and "StandardOutput=socket" in service
        assert "sh -c" not in service and "NoNewPrivileges=yes" in service and "ProtectHome=yes" in service
        assert "ProtectSystem=strict" in service and "RuntimeMaxSec=" in service
    systemd_service = (DEPLOYMENT / "systemd" / "atlas-mcp-systemd@.service").read_text()
    exec_start = next(line for line in systemd_service.splitlines() if line.startswith("ExecStart="))
    assert "--enabled-tools ${SYSTEMD_MCP_ENABLED_TOOLS}" in exec_start  # the owner's policy decides which tools exist
    assert "EnvironmentFile=/etc/atlas-v5/config/atlas-mcp-systemd.env" in systemd_service
    shell_service = (DEPLOYMENT / "systemd" / "atlas-mcp-shell@.service").read_text()
    assert "EnvironmentFile=/etc/atlas-v5/config/atlas-mcp-shell.env" in shell_service


def test_host_operations_installer_uses_structured_root_broker_without_polkit_policy() -> None:
    installer = (DEPLOYMENT / "install-host-mcp.sh").read_text()
    assert '[[ ${EUID} -eq 0 ]]' in installer
    assert "atlas_host_operations_server.py" in installer
    assert '"${BIN_DIR}/atlas_mcp_probe.py"' in installer
    assert 'runuser -u atlas-v5 -- /usr/bin/python3 "${BIN_DIR}/atlas_mcp_probe.py"' in installer
    assert "enable --now atlas-host-operations.socket" in installer
    assert "rm -f /etc/polkit-1/rules.d/50-atlas-tools.rules" in installer
    assert "pkaction" not in installer and "systemd-mcp" not in installer and "mcp-shell-server" not in installer
    assert "ATLAS_MCP_SERVERS_FILE" in installer and "ATLAS_HOST_POLICY_FILE" in installer and "unset_env" in installer
    assert "host-scopes.json" in installer
    broker = (DEPLOYMENT / "host-mcp" / "atlas_host_operations_server.py").read_text()
    assert "shell=True" not in broker and "subprocess.run(argv" in broker
    assert "filesystem_write" in broker and "packages_change" in broker and "host_shutdown" in broker
    service = (DEPLOYMENT / "systemd" / "atlas-host-operations@.service").read_text()
    assert "User=root" in service and "ExecStart=/usr/bin/python3 /opt/atlas-v5/bin/atlas_host_operations_server.py" in service
    socket = (DEPLOYMENT / "systemd" / "atlas-host-operations.socket").read_text()
    assert "SocketGroup=atlas-v5" in socket and "SocketMode=0660" in socket and "Accept=yes" in socket
    deploy = (DEPLOYMENT / "deploy-host.sh").read_text()
    assert deploy.index("install-host-mcp.sh") > deploy.index("alembic upgrade head")
    assert deploy.index("install-host-mcp.sh") < deploy.index("systemctl start atlas-v5.service")


def test_proving_case_artifacts_are_safe_and_reproducible() -> None:
    apply = (DEPLOYMENT / "host" / "desktop-commander" / "apply.sh").read_text()
    assert 'if [[ ${EUID} -eq 0 ]]' in apply and "EXPECTED_VERSION=0.2.48" in apply
    assert apply.index("--dry-run") < apply.index("patch -p1 -b -z .atlas-orig")
    patch_text = (DEPLOYMENT / "host" / "desktop-commander" / "desktop-commander-0.2.48-persist-session.patch").read_text()
    touched = {line.split()[1] for line in patch_text.splitlines() if line.startswith("+++ ")}
    assert touched == {"b/dist/remote-device/device.js", "b/dist/remote-device/remote-channel.js"}
    assert "onSessionRefreshed" in patch_text and "savePersistedConfig" in patch_text
    for name in ("desktop-commander", "empire-control", "empire-account-portal"):
        unit = (DEPLOYMENT / "host" / "units" / f"{name}.service").read_text()
        assert "User=jaco" in unit and "WantedBy=multi-user.target" in unit and "sudo" not in unit
    connector = (DEPLOYMENT / "host" / "units" / "desktop-commander.service").read_text()
    assert "StartLimitIntervalSec=1h" in connector and "StartLimitBurst=3" in connector
    converter = (DEPLOYMENT / "host" / "install-owner-units.sh").read_text()
    assert 'if [[ ${EUID} -ne 0 ]]' in converter and "ATLAS_RDC_MONITOR_SCOPE=system" in converter
    assert ".disabled" in converter and "enable --now" in converter


def test_host_authority_is_runtime_owned_and_broker_has_no_policy_engine() -> None:
    installer = (DEPLOYMENT / "install-host-mcp.sh").read_text()
    assert "Authority is controlled in Atlas Control" in installer
    assert "polkit is not part of this execution path" in installer
    mcp = (DEPLOYMENT / "mcp-servers.example.toml").read_text()
    assert 'id = "host.operations"' in mcp
    assert "owner authority lives in control / operation_authority" in mcp.lower()
    assert "authority_rules" not in mcp
    broker = (DEPLOYMENT / "host-mcp" / "atlas_host_operations_server.py").read_text()
    assert "operation_authority" not in broker and "approval_required" not in broker
    unit = (DEPLOYMENT / "systemd" / "atlas-v5.service").read_text()
    assert "ReadWritePaths=/var/lib/atlas-v5" in unit


def test_observer_state_is_separate_and_owner_read_only() -> None:
    deploy = (DEPLOYMENT / "deploy-host.sh").read_text()
    bootstrap = (DEPLOYMENT / "bootstrap-host.sh").read_text()
    grant = (DEPLOYMENT / "grant-maintenance-access.sh").read_text()
    observer = "/var/lib/atlas-v5/observer"
    assert observer in deploy
    assert observer in bootstrap
    assert f"OBSERVER_ROOT={observer}" in grant
    assert 'u:${MAINTAINER}:r-x' in grant
    assert 'u:${MAINTAINER}:r--' in grant
    assert "/etc/atlas-v5" not in "\n".join(
        line for line in grant.splitlines() if "setfacl" in line
    )
