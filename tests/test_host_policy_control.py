"""Host policy from Control: read, validate, stage, and report; the runtime never writes the effective files."""
import json
from pathlib import Path

import httpx
import pytest
from atlas.api.app import app
from atlas.config import Settings
from atlas.control.host_policy import (
    REQUEST,
    RESULT,
    STAGED_POLICY,
    STAGED_SERVERS,
    HostPolicyControl,
    staging_dir,
)

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_POLICY = (ROOT / "deployment" / "host-mcp" / "host-policy.example.toml").read_text()
EXAMPLE_SERVERS = (ROOT / "deployment" / "mcp-servers.example.toml").read_text()


def _settings(tmp_path: Path) -> Settings:
    config = tmp_path / "etc"
    config.mkdir()
    (config / "host-policy.toml").write_text(EXAMPLE_POLICY)
    (config / "mcp-servers.toml").write_text(EXAMPLE_SERVERS)
    return Settings(state_dir=tmp_path / "state", host_policy_file=config / "host-policy.toml",
        mcp_servers_file=config / "mcp-servers.toml", host_policy_renderer=tmp_path / "missing-renderer.py")


def test_status_reports_effective_files_with_rendered_envelope(tmp_path) -> None:
    control = HostPolicyControl(_settings(tmp_path))
    status = control.status()
    assert status["policy"]["exists"] and status["policy"]["text"] == EXAMPLE_POLICY
    effective = status["policy"]["effective"]
    assert effective["ok"] and '"com.suse.gatekeeper.readlog": ["atlas-tools"]' in effective["rule"]
    assert effective["units"]["desktop-commander.service"] == ["start", "stop", "restart", "reset-failed"]
    assert status["servers"]["effective"]["ok"] and {s["id"] for s in status["servers"]["effective"]["servers"]} == {"host.systemd", "host.shell"}
    assert status["pending"] is None and status["last_result"] is None


def test_validate_explains_rejections_and_notes_without_writing(tmp_path) -> None:
    control = HostPolicyControl(_settings(tmp_path))
    bad = control.validate_policy('[shell]\nallow_commands = ["rm"]\n')
    assert bad["ok"] is False and "allow_risky_commands" in bad["error"]
    noted = control.validate_policy('[identity]\ngroups = ["systemd-journal", "docker"]\n[shell]\nallow_commands = ["ls"]\n')
    assert noted["ok"] and any("docker" in note for note in noted["notes"])
    servers = control.validate_servers('[[servers]]\nid = "Bad"\n')
    assert servers["ok"] is False and "id must look like" in servers["error"]
    assert not staging_dir(control.settings).exists()


def test_stage_validates_then_writes_files_and_request(tmp_path) -> None:
    control = HostPolicyControl(_settings(tmp_path))
    with pytest.raises(ValueError, match="rejected"):
        control.stage(policy_text="not = toml =")
    with pytest.raises(ValueError, match="nothing to apply"):
        control.stage()
    request = control.stage(policy_text=EXAMPLE_POLICY, servers_text=EXAMPLE_SERVERS)
    staging = staging_dir(control.settings)
    assert request["kinds"] == ["policy", "servers"]
    assert (staging / STAGED_POLICY).read_text() == EXAMPLE_POLICY and (staging / STAGED_SERVERS).read_text() == EXAMPLE_SERVERS
    assert json.loads((staging / REQUEST).read_text())["kinds"] == ["policy", "servers"]
    assert oct(staging.stat().st_mode & 0o777) == "0o700" and oct((staging / STAGED_POLICY).stat().st_mode & 0o777) == "0o600"
    # The effective file is untouched; only root's apply unit changes it.
    assert control.settings.host_policy_file.read_text() == EXAMPLE_POLICY
    assert control.status()["pending"]["kinds"] == ["policy", "servers"]
    (staging / RESULT).write_text(json.dumps({"status": "applied", "applied": ["host-policy.toml"], "errors": [], "notes": [], "restart_required": False}))
    (staging / REQUEST).unlink()
    status = control.status()
    assert status["pending"] is None and status["last_result"]["status"] == "applied"


async def _request(method: str, path: str, **kwargs):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.request(method, path, **kwargs)


@pytest.mark.asyncio
async def test_control_api_reads_validates_and_stages(tmp_path, monkeypatch) -> None:
    import atlas.api.app as app_module

    settings = _settings(tmp_path)
    for name in ("state_dir", "host_policy_file", "mcp_servers_file", "host_policy_renderer"):
        monkeypatch.setattr(app_module.settings, name, getattr(settings, name))
    status = await _request("GET", "/api/control/host")
    assert status.status_code == 200 and status.json()["policy"]["effective"]["ok"] is True
    verdict = await _request("POST", "/api/control/host/validate", json={"policy": '[shell]\nallow_commands = ["bash"]\n'})
    assert verdict.status_code == 200 and verdict.json()["policy"]["ok"] is False
    rejected = await _request("PUT", "/api/control/host", json={"policy": '[shell]\nallow_commands = ["bash"]\n'})
    assert rejected.status_code == 422 and "allow_risky_commands" in rejected.json()["detail"]
    staged = await _request("PUT", "/api/control/host", json={"servers": EXAMPLE_SERVERS})
    assert staged.status_code == 200 and staged.json()["pending"]["kinds"] == ["servers"]
    assert (staging_dir(settings) / STAGED_SERVERS).is_file()


@pytest.mark.asyncio
async def test_host_policy_routes_sit_behind_the_owner_boundary(monkeypatch) -> None:
    import atlas.api.app as app_module

    monkeypatch.setattr(app_module.settings, "auth_required", True)
    assert (await _request("GET", "/api/control/host")).status_code == 401
    assert (await _request("PUT", "/api/control/host", json={"policy": "x"})).status_code == 401
