from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOYMENT = ROOT / "deployment"


def test_normal_deploy_does_not_walk_owner_projects() -> None:
    script = (DEPLOYMENT / "deploy-host.sh").read_text()
    assert "find /home/jaco/Projects" not in script
    assert "reconcile-project-access.sh" in script


def test_maintenance_acl_grant_is_confined_to_deployment_root() -> None:
    script = (DEPLOYMENT / "grant-maintenance-access.sh").read_text()
    acl_lines = [line for line in script.splitlines() if "setfacl" in line and "find" in line]
    assert acl_lines
    assert all('${APP_ROOT}' in line for line in acl_lines)
    assert "APP_ROOT=/opt/atlas-v5" in script


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
