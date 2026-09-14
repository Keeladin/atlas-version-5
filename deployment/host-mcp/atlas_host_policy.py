#!/usr/bin/env python3
"""Render the owner's host policy into the OS envelope: a polkit rule, the shell server's
environment, and the systemd server's environment.

The owner decides. This module validates shapes (names, types, bounds) and renders what the
policy says; it does not hold a list of things the owner may not do. Where a choice carries
real risk the policy has an explicit opt-in flag whose name appears in the rejection message.
Standard library only; runs as root from install-host-mcp.sh and from the tests by path.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path

UNIT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@:\\-]{0,254}\.(service|timer|socket|target|path|mount|scope|slice)$")
USER_NAME = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
GROUP_NAME = USER_NAME
COMMAND_NAME = re.compile(r"^[a-z0-9][a-z0-9_.+-]{0,63}$")
POLKIT_ACTION = re.compile(r"^[a-z][a-z0-9-]*(\.[a-z][a-z0-9-]*)+$")
TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

# systemd passes `unit` and `verb` to polkit only for manage-units; unit-file changes are
# authorized per subject, not per unit, so they are granted globally when any unit lists them.
MANAGE_UNIT_VERBS = ("start", "stop", "restart", "reload", "try-restart", "reload-or-restart", "reset-failed", "kill", "set-property")
UNIT_FILE_VERBS = ("enable", "disable", "mask", "unmask", "preset", "link", "revert")
MANAGE_UNITS_ACTION = "org.freedesktop.systemd1.manage-units"
MANAGE_UNIT_FILES_ACTION = "org.freedesktop.systemd1.manage-unit-files"

DEFAULT_GROUPS = ("systemd-journal",)
DEFAULT_SYSTEMD_TOOLS = ("list_loaded_units", "list_unit_files", "change_unit_state", "check_restart_reload", "list_log")
KNOWN_SYSTEMD_TOOLS = (*DEFAULT_SYSTEMD_TOOLS, "get_file", "get_man_page")

# Commands that can write, escalate or interpret. They are not forbidden; listing one needs
# `allow_risky_commands = true` so the choice is explicit.
RISKY_COMMANDS = frozenset({
    "sh", "bash", "dash", "zsh", "fish", "ksh", "csh", "tcsh", "busybox", "env", "xargs", "sudo", "su", "doas", "pkexec",
    "python", "python3", "perl", "ruby", "node", "npm", "npx", "uv", "uvx", "pip", "pip3", "awk", "gawk", "mawk", "sed",
    "vi", "vim", "nano", "ed", "tee", "dd", "rm", "mv", "cp", "chmod", "chown", "chgrp", "ln", "mkdir", "rmdir", "touch",
    "truncate", "shred", "mkfs", "mount", "umount", "docker", "podman", "nsenter", "chroot", "systemd-run", "at", "crontab",
    "curl", "wget", "nc", "ncat", "netcat", "socat", "ssh", "scp", "sftp", "rsync", "git", "apt", "apt-get", "dpkg", "snap",
    "reboot", "shutdown", "poweroff", "halt", "kill", "pkill", "killall", "loginctl", "machinectl", "busctl", "gdb", "strace",
})


class PolicyError(ValueError):
    pass


def _string_list(value, *, where: str, pattern: re.Pattern, what: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise PolicyError(f"{where} must be a list of strings")
    items: list[str] = []
    for item in value:
        if not pattern.match(item) or "/" in item:
            raise PolicyError(f"{where}: invalid {what} {item!r}")
        if item not in items:
            items.append(item)
    return items


def load_policy(text: str) -> dict:
    data = tomllib.loads(text)
    identity = data.get("identity") or {}
    user = str(identity.get("user") or "atlas-tools")
    owner = identity.get("owner_user")
    for name in (user, owner) if owner else (user,):
        if not USER_NAME.match(str(name)):
            raise PolicyError(f"identity: invalid user name {name!r}")
    groups = _string_list(identity.get("groups", list(DEFAULT_GROUPS)), where="identity.groups", pattern=GROUP_NAME, what="group")

    units: dict[str, list[str]] = {}
    unit_file_grant = False
    for index, item in enumerate(data.get("managed_units") or []):
        if not isinstance(item, dict):
            raise PolicyError(f"managed_units[{index}]: must be a table")
        unit = str(item.get("unit") or "")
        if not UNIT_NAME.match(unit):
            raise PolicyError(f"managed_units[{index}]: invalid unit name {unit!r}")
        if unit in units:
            raise PolicyError(f"managed_units[{index}]: duplicate unit {unit}")
        verbs = item.get("verbs")
        if not isinstance(verbs, list) or not verbs:
            raise PolicyError(f"managed_units[{index}]: verbs must be a non-empty list")
        for verb in verbs:
            if verb not in MANAGE_UNIT_VERBS and verb not in UNIT_FILE_VERBS:
                raise PolicyError(f"managed_units[{index}]: unknown verb {verb!r}; systemd verbs are {', '.join(MANAGE_UNIT_VERBS + UNIT_FILE_VERBS)}")
            if verb in UNIT_FILE_VERBS:
                unit_file_grant = True
        units[unit] = list(dict.fromkeys(str(verb) for verb in verbs))

    polkit = data.get("polkit") or {}
    if "extra_actions" in polkit:
        raise PolicyError("polkit.extra_actions was replaced by [[polkit.grants]] entries with an explicit identity list")
    subjects = [user] + ([str(owner)] if owner else [])
    grants: dict[str, list[str]] = {}
    for index, item in enumerate(polkit.get("grants") or []):
        where = f"polkit.grants[{index}]"
        if not isinstance(item, dict):
            raise PolicyError(f"{where}: must be a table")
        action = str(item.get("action") or "")
        if not POLKIT_ACTION.match(action):
            raise PolicyError(f"{where}: invalid polkit action id {action!r}")
        if action in grants:
            raise PolicyError(f"{where}: duplicate grant for {action}")
        identities = _string_list(item.get("identities", [user]), where=f"{where}.identities", pattern=USER_NAME, what="user name")
        if not identities:
            raise PolicyError(f"{where}: identities must name at least one of {', '.join(subjects)}")
        for identity in identities:
            if identity not in subjects:
                raise PolicyError(f"{where}: identity {identity!r} is not the tools user or the owner ({', '.join(subjects)})")
        grants[action] = identities

    shell = data.get("shell") or {}
    allow_risky = bool(shell.get("allow_risky_commands", False))
    commands = _string_list(shell.get("allow_commands"), where="shell.allow_commands", pattern=COMMAND_NAME, what="command name")
    risky = [command for command in commands if command in RISKY_COMMANDS]
    if risky and not allow_risky:
        raise PolicyError(f"shell.allow_commands lists commands that can write, escalate or interpret ({', '.join(risky)}); "
            "set shell.allow_risky_commands = true to grant them deliberately")

    def bounded(table: dict, key: str, default: int, low: int, high: int, *, where: str) -> int:
        value = table.get(key, default)
        if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
            raise PolicyError(f"{where}.{key} must be an integer between {low} and {high}")
        return value

    systemd = data.get("systemd") or {}
    enabled_tools = _string_list(systemd.get("enabled_tools", list(DEFAULT_SYSTEMD_TOOLS)), where="systemd.enabled_tools", pattern=TOOL_NAME, what="tool name")
    unknown_tools = [tool for tool in enabled_tools if tool not in KNOWN_SYSTEMD_TOOLS]

    warnings: list[str] = []
    if unit_file_grant:
        warnings.append(f"unit-file verbs are listed: polkit cannot scope {MANAGE_UNIT_FILES_ACTION} per unit, so it is granted for every unit")
    if risky:
        warnings.append(f"risky commands granted deliberately: {', '.join(risky)}")
    if "docker" in groups:
        warnings.append("identity.groups includes docker, which is root-equivalent on this host")
    if unknown_tools:
        warnings.append(f"systemd.enabled_tools names tools this policy does not know: {', '.join(unknown_tools)}")
    if "get_file" in enabled_tools:
        warnings.append("get_file lets the model read any file the tools identity can read")

    return {
        "user": user, "owner_user": str(owner) if owner else None, "groups": groups, "units": units,
        "unit_file_grant": unit_file_grant, "grants": grants,
        "shell_commands": commands, "allow_risky_commands": allow_risky,
        "default_timeout_seconds": bounded(shell, "default_timeout_seconds", 20, 1, 3600, where="shell"),
        "max_timeout_seconds": bounded(shell, "max_timeout_seconds", 60, 1, 3600, where="shell"),
        "output_limit_bytes": bounded(shell, "output_limit_bytes", 262144, 1024, 16 * 1024 * 1024, where="shell"),
        "systemd_tools": enabled_tools,
        "polkit_timeout_seconds": bounded(systemd, "polkit_timeout_seconds", 5, 1, 60, where="systemd"),
        "warnings": warnings,
    }


def render_polkit_rules(policy: dict) -> str:
    subjects = [policy["user"]] + ([policy["owner_user"]] if policy["owner_user"] else [])
    unit_verbs = {unit: [verb for verb in verbs if verb in MANAGE_UNIT_VERBS] for unit, verbs in policy["units"].items()}
    grants: dict[str, list[str]] = {}
    if policy["unit_file_grant"]:
        grants[MANAGE_UNIT_FILES_ACTION] = list(subjects)
    grants.update(policy["grants"])
    lines = [
        "// Generated by Atlas from /etc/atlas-v5/config/host-policy.toml. Do not edit; edit the policy and re-run install-host-mcp.sh.",
        "polkit.addRule(function(action, subject) {",
        f"    var subjects = {json.dumps(subjects)};",
        "    if (subjects.indexOf(subject.user) < 0) {",
        "        return polkit.Result.NOT_HANDLED;",
        "    }",
        f'    if (action.id == "{MANAGE_UNITS_ACTION}") {{',
        f"        var units = {json.dumps(unit_verbs, sort_keys=True)};",
        '        var verbs = units[action.lookup("unit")];',
        '        if (verbs && verbs.indexOf(action.lookup("verb")) >= 0) {',
        "            return polkit.Result.YES;",
        "        }",
        "        return polkit.Result.NOT_HANDLED;",
        "    }",
    ]
    if grants:
        lines += [
            "    // Per-action grants, each limited to the identities the policy names.",
            f"    var grants = {json.dumps(grants, sort_keys=True)};",
            "    var identities = grants[action.id];",
            "    if (identities && identities.indexOf(subject.user) >= 0) {",
            "        return polkit.Result.YES;",
            "    }",
        ]
    lines += ["    return polkit.Result.NOT_HANDLED;", "});", ""]
    return "\n".join(lines)


def render_shell_env(policy: dict) -> str:
    return (
        "# Generated by Atlas from /etc/atlas-v5/config/host-policy.toml. Do not edit.\n"
        f"ALLOW_COMMANDS={','.join(policy['shell_commands'])}\n"
        f"MCP_SHELL_DEFAULT_TIMEOUT_SECONDS={policy['default_timeout_seconds']}\n"
        f"MCP_SHELL_MAX_TIMEOUT_SECONDS={policy['max_timeout_seconds']}\n"
        f"MCP_SHELL_OUTPUT_LIMIT_BYTES={policy['output_limit_bytes']}\n"
        "MCP_SHELL_CHILD_ENV_ALLOWLIST=\n"
        "MCP_SHELL_SAFE_PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin\n"
    )


def render_systemd_env(policy: dict) -> str:
    return (
        "# Generated by Atlas from /etc/atlas-v5/config/host-policy.toml. Do not edit.\n"
        f"SYSTEMD_MCP_ENABLED_TOOLS={','.join(policy['systemd_tools'])}\n"
        f"SYSTEMD_MCP_POLKIT_TIMEOUT={policy['polkit_timeout_seconds']}\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render the Atlas host policy")
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--polkit-out", type=Path)
    parser.add_argument("--shell-env-out", type=Path)
    parser.add_argument("--systemd-env-out", type=Path)
    parser.add_argument("--print-units", action="store_true", help="print the managed units, comma separated")
    parser.add_argument("--print-groups", action="store_true", help="print the tools identity's groups, comma separated")
    parser.add_argument("--check", action="store_true", help="validate and summarise only")
    args = parser.parse_args(argv)
    try:
        policy = load_policy(args.policy.read_text())
    except (OSError, PolicyError, tomllib.TOMLDecodeError) as exc:
        print(f"host policy rejected: {exc}", file=sys.stderr)
        return 2
    if args.print_units:
        print(",".join(policy["units"]))
        return 0
    if args.print_groups:
        print(",".join(policy["groups"]))
        return 0
    for warning in policy["warnings"]:
        print(f"host policy note: {warning}", file=sys.stderr)
    if args.check:
        print(f"host policy ok: {len(policy['units'])} units, {len(policy['shell_commands'])} commands, "
            f"{len(policy['systemd_tools'])} systemd tools, tools user {policy['user']} in {','.join(policy['groups']) or 'no extra groups'}")
        return 0
    if args.polkit_out:
        args.polkit_out.write_text(render_polkit_rules(policy))
    if args.shell_env_out:
        args.shell_env_out.write_text(render_shell_env(policy))
    if args.systemd_env_out:
        args.systemd_env_out.write_text(render_systemd_env(policy))
    return 0


if __name__ == "__main__":
    sys.exit(main())
