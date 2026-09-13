"""The host policy renderer: the owner's policy becomes the polkit rule and the server environments."""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "deployment" / "host-mcp" / "atlas_host_policy.py"
EXAMPLE = ROOT / "deployment" / "host-mcp" / "host-policy.example.toml"

spec = importlib.util.spec_from_file_location("atlas_host_policy", MODULE)
policy_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy_module)


def test_example_policy_renders_scoped_polkit_rule_and_server_envs() -> None:
    policy = policy_module.load_policy(EXAMPLE.read_text())
    assert policy["user"] == "atlas-tools" and policy["owner_user"] == "jaco" and policy["groups"] == ["systemd-journal"]
    assert set(policy["units"]) == {"desktop-commander.service", "empire-control.service", "empire-account-portal.service"}
    assert policy["warnings"] == []
    assert policy["grants"] == {"com.suse.gatekeeper.readlog": ["atlas-tools"]}  # systemd-mcp's read gate, tools identity only
    rules = policy_module.render_polkit_rules(policy)
    assert 'action.id == "org.freedesktop.systemd1.manage-units"' in rules
    assert "manage-unit-files" not in rules and "reload-daemon" not in rules
    assert json.dumps(["atlas-tools", "jaco"]) in rules
    assert '"desktop-commander.service": ["start", "stop", "restart", "reset-failed"]' in rules
    assert '{"com.suse.gatekeeper.readlog": ["atlas-tools"]}' in rules
    assert "polkit.Result.NOT_HANDLED" in rules and rules.count("polkit.Result.YES") == 2
    env = policy_module.render_shell_env(policy)
    assert "ALLOW_COMMANDS=journalctl,systemctl,ps," in env and "MCP_SHELL_CHILD_ENV_ALLOWLIST=\n" in env
    assert "MCP_SHELL_OUTPUT_LIMIT_BYTES=262144" in env and "bash" not in env
    systemd_env = policy_module.render_systemd_env(policy)
    assert "SYSTEMD_MCP_ENABLED_TOOLS=list_loaded_units,list_unit_files,change_unit_state,check_restart_reload,list_log\n" in systemd_env
    assert "get_file" not in systemd_env and "SYSTEMD_MCP_POLKIT_TIMEOUT=5" in systemd_env


def test_owner_can_grant_more_with_explicit_choices_and_is_told_what_it_means() -> None:
    policy = policy_module.load_policy('''
[identity]
owner_user = "jaco"
groups = ["systemd-journal", "docker"]
[[managed_units]]
unit = "a.service"
verbs = ["restart", "enable", "disable"]
[[polkit.grants]]
action = "org.freedesktop.systemd1.reload-daemon"
identities = ["atlas-tools", "jaco"]
[systemd]
enabled_tools = ["change_unit_state", "get_file"]
[shell]
allow_commands = ["ls", "docker", "bash"]
allow_risky_commands = true
max_timeout_seconds = 900
''')
    assert policy["groups"] == ["systemd-journal", "docker"] and policy["unit_file_grant"] is True
    assert policy["shell_commands"] == ["ls", "docker", "bash"] and policy["max_timeout_seconds"] == 900
    notes = "\n".join(policy["warnings"])
    assert "manage-unit-files" in notes and "docker" in notes and "get_file" in notes and "risky commands granted deliberately" in notes
    rules = policy_module.render_polkit_rules(policy)
    assert '"a.service": ["restart"]' in rules  # unit-scoped verbs only
    assert '"org.freedesktop.systemd1.manage-unit-files": ["atlas-tools", "jaco"]' in rules
    assert '"org.freedesktop.systemd1.reload-daemon": ["atlas-tools", "jaco"]' in rules
    assert rules.count("polkit.Result.YES") == 2
    assert "SYSTEMD_MCP_ENABLED_TOOLS=change_unit_state,get_file" in policy_module.render_systemd_env(policy)
    assert "ALLOW_COMMANDS=ls,docker,bash" in policy_module.render_shell_env(policy)


@pytest.mark.parametrize("snippet,message", [
    ('[[managed_units]]\nunit = "a.service"\nverbs = ["daemon-reload"]\n', "unknown verb"),
    ('[[managed_units]]\nunit = "a; rm -rf /"\nverbs = ["start"]\n', "invalid unit name"),
    ('[[managed_units]]\nunit = "a.service"\nverbs = []\n', "non-empty"),
    ('[[managed_units]]\nunit = "a.service"\nverbs = ["start"]\n[[managed_units]]\nunit = "a.service"\nverbs = ["stop"]\n', "duplicate"),
    ('[shell]\nallow_commands = ["bash"]\n', "allow_risky_commands = true"),
    ('[shell]\nallow_commands = ["docker"]\n', "allow_risky_commands = true"),
    ('[shell]\nallow_commands = ["/usr/bin/ls"]\n', "invalid command name"),
    ('[shell]\nallow_commands = ["ls"]\nmax_timeout_seconds = 99999\n', "between"),
    ('[identity]\nuser = "Not A User"\n', "invalid user"),
    ('[identity]\ngroups = ["bad group"]\n', "invalid group"),
    ('[[polkit.grants]]\naction = "not-an-action"\n', "invalid polkit action"),
    ('[[polkit.grants]]\naction = "com.suse.gatekeeper.readlog"\nidentities = ["root"]\n', "not the tools user or the owner"),
    ('[[polkit.grants]]\naction = "com.suse.gatekeeper.readlog"\nidentities = []\n', "at least one"),
    ('[polkit]\nextra_actions = ["x.y"]\n', "replaced by"),
    ('[systemd]\nenabled_tools = ["get file"]\n', "invalid tool"),
])
def test_policy_shape_rejections(snippet, message) -> None:
    with pytest.raises(policy_module.PolicyError, match=message):
        policy_module.load_policy(snippet)


def test_cli_check_print_and_render(tmp_path, capsys) -> None:
    policy = tmp_path / "policy.toml"
    policy.write_text(EXAMPLE.read_text())
    assert policy_module.main(["--policy", str(policy), "--check"]) == 0
    assert policy_module.main(["--policy", str(policy), "--print-groups"]) == 0
    assert "systemd-journal" in capsys.readouterr().out
    rules, shell_env, systemd_env = tmp_path / "rules", tmp_path / "shell.env", tmp_path / "systemd.env"
    assert policy_module.main(["--policy", str(policy), "--polkit-out", str(rules), "--shell-env-out", str(shell_env), "--systemd-env-out", str(systemd_env)]) == 0
    assert rules.read_text().startswith("// Generated by Atlas") and "ALLOW_COMMANDS=" in shell_env.read_text()
    assert "SYSTEMD_MCP_ENABLED_TOOLS=" in systemd_env.read_text()
    bad = tmp_path / "bad.toml"
    bad.write_text('[shell]\nallow_commands = ["rm"]\n')
    assert policy_module.main(["--policy", str(bad), "--check"]) == 2
    assert "allow_risky_commands" in capsys.readouterr().err
