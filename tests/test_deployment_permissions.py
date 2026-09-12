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
