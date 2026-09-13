import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "deployment" / "host-mcp" / "atlas_host_operations_server.py"
spec = importlib.util.spec_from_file_location("atlas_host_operations_server", PATH)
assert spec and spec.loader
broker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(broker)


def test_tool_surface_is_structured_and_matches_owner_matrix() -> None:
    names = {item["name"] for item in broker.TOOLS}
    assert names == {
        "services_inspect", "service_logs", "service_start", "service_restart", "service_stop", "service_enable_disable",
        "docker_inspect", "docker_start_restart", "docker_stop", "docker_create_remove",
        "filesystem_read", "filesystem_write", "filesystem_delete", "packages_inspect", "packages_change",
        "host_resources", "host_restart", "host_shutdown",
    }
    assert all("shell" not in name for name in names)


def test_filesystem_scope_fails_closed_when_no_paths(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(broker, "SCOPES", tmp_path / "missing.json")
    with pytest.raises(PermissionError, match="No filesystem read paths"):
        broker.allowed("/etc/hosts", "read")


def test_filesystem_scope_accepts_descendant_and_rejects_escape(monkeypatch, tmp_path: Path) -> None:
    base = tmp_path / "allowed"
    base.mkdir()
    scopes = tmp_path / "scopes.json"
    scopes.write_text('{"read":["%s"],"write":[],"delete":[]}'.replace("%s", str(base)))
    monkeypatch.setattr(broker, "SCOPES", scopes)
    assert broker.allowed(str(base / "a.txt"), "read") == (base / "a.txt").resolve()
    with pytest.raises(PermissionError, match="outside"):
        broker.allowed(str(tmp_path / "other.txt"), "read")


def test_service_actions_are_fixed_argv(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(broker, "run", lambda argv, timeout=120: calls.append(argv) or {"returncode": 0})
    broker.call("service_restart", {"unit": "example.service"})
    broker.call("service_enable_disable", {"unit": "example.service", "enabled": False})
    assert calls == [["systemctl", "restart", "example.service"], ["systemctl", "disable", "example.service"]]

def test_package_inspect_rejects_option_injection(monkeypatch) -> None:
    monkeypatch.setattr(broker, "run", lambda argv, timeout=120: {"argv": argv})
    with pytest.raises(ValueError, match="invalid package"):
        broker.call("packages_inspect", {"package": "--option"})
