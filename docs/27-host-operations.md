# Atlas V5 Host Operations

## Principle

Atlas looks after the host it runs on the way it looks after anything else: it becomes aware of an event, reasons about it, acts through governed tools under authority the owner configured, verifies the result, and keeps the owner informed. The capability is generic. No service, playbook or command sequence is written into Atlas; the Desktop Commander connector was only the first case that proved it.

Two rules from the Runtime Constitution shape everything below: model-facing execution runs in a boundary **less** privileged than the runtime and never reaches runtime secrets (`17-runtime-constitution.md` §3–4, `18-implementation-plan.md`), and consequential effects carry durable action identity with evidence, without a general confirmation state (`10-authority-and-control.md`).

## Executor choice

Existing MCP servers were preferred over building an executor (`08-capability-map.md`). Candidates were evaluated from source, not listings:

| Server | Verdict |
| --- | --- |
| `pazuzu1w/ubuntu_mcp_server` | basename allow lists only, no argument policy, no identity model, unmaintained since June 2025. Not adopted. |
| `openSUSE/systemd-mcp` | unit control authorized by **polkit**, which scopes by unit and verb; journal read for `systemd-journal` members; `--enabled-tools` drops its file reader. **Adopted** for unit control. |
| `tumf/mcp-shell-server` | argv only, no shell, allow lists and argument hardening, empty child environment, structured audit with redaction. **Adopted** for read-only diagnostics. |
| `mvo5/systemd-mcp` | 50–80 unguarded varlink tools, needs systemd 260. Not adopted. |
| Desktop Commander MCP | deny list only, and Atlas must not depend on the thing it may be repairing. Not part of host control. |

Identity switching and argument-level authority, which none of them provide, come from the operating system.

## The five layers

### 1. Awareness

The notification ledger (`26-notifications.md`) is the event bus. Deterministic monitors in the runtime emit structured events; nothing model-side polls for trouble.

- `backend/atlas/monitors/units.py` watches every unit in `ATLAS_HOST_WATCH_UNITS` with `systemctl show` (read-only, argv, no privilege), persists per-unit state in `host_monitor_state`, and emits `warning` when a unit is failed or inactive (`host.unit:<unit>`), `info` when its restart count rises, and `resolved` when it is active again. Steady state emits nothing.
- `backend/atlas/monitors/rdc.py` remains the specialized connector monitor (device-code flow) and follows the unit into the system journal with `ATLAS_RDC_MONITOR_SCOPE=system`.

### 2. Wake

`schedule_kind = "event"` binds a standing owner prompt to a filter over notifications:

```json
{"source": ["runtime.units", "runtime.rdc"], "severity": ["warning", "action_required", "critical"], "kind": ["unit_unhealthy"], "thread_prefix": "host.unit:"}
```

`source` is required; `severity` defaults to warning/action_required/critical; `kind` and `thread_prefix` are optional. The model's own notifications wake a task only when its filter says `"include_model": true`, and a task's own runs never wake it. `claim_events` (`backend/atlas/schedules/runner.py`) queues one run per matching task and notification, carrying the task prompt and the **model-audience, redacted** projection of the event in `trigger_snapshot`; the run's user message is the standing intent followed by the event as data. While a task has an outstanding run, later notifications wait for the next poll (coalescing). Superseded, stale (older than six hours), pre-task and self-originated notifications are claimed without a run. `notifications.wake_claimed_at` records the decision atomically with the queued runs (migration `25a15`).

Playbooks live in the prompt, which the owner edits through conversation or Control, never in code.

### 3. Reasoning with governed tools

Any MCP server can be a governed capability through `ATLAS_MCP_SERVERS_FILE` (`/etc/atlas-v5/config/mcp-servers.toml`, example in `deployment/mcp-servers.example.toml`):

```toml
[[servers]]
id = "host.systemd"               # capability id; operations are host.systemd.<tool>
family = "Host services"
transport = "socket"              # or "stdio" with command/args/env_file
path = "/run/atlas-v5/mcp/systemd.sock"
tools = ["list_loaded_units", "change_unit_state", "list_log"]   # allowlist; unlisted tools never exist for Atlas
[servers.defaults]
read_only_authority = "auto"
destructive_authority = "approval_required"
non_destructive_authority = "auto"
[servers.tool.change_unit_state]
effect = "execute"
authority = "auto"
authority_rules = [ { when = { action = ["stop", "disable"] }, authority = "approval_required" } ]
```

At startup `register_mcp_servers` (`backend/atlas/integrations/mcp_servers.py`) discovers each server's tools, derives effect and authority (MCP `readOnlyHint` → read/auto; otherwise execute with `destructiveHint` absent-or-true → approval, false → auto; explicit configuration always wins because annotations are hints from an external process), and registers one capability entry and one operation per tool. New capability rows start **disabled** and the owner enables them in Control. The socket transport (`MCPSocketClient`) is one connection per request, the same choreography as the stdio client.

### 4. Owner-configurable authority

- **In Control:** the Authority card lists every registered operation with its default and lets the owner set it to Auto, Ask me, or Forbidden. That decision is stored in `operation_authority` (migration `25a16`), read by the runtime on every call, and wins over the descriptor, the server's hints, the MCP configuration and any argument rule. "Default" removes the decision. `/api/control/operations` is the same surface for scripts.
- **In configuration:** `authority_rules` map argument values to authority per call for operations the owner has not decided on. `CapabilityRuntime.resolve_authority` runs after validation and before the approval check; the executor resolves once per tool call so the durable action row and the dispatch agree. Approval-required calls become proposals in Needs You exactly like any other consequential effect.
- **In the OS:** one policy file, `/etc/atlas-v5/config/host-policy.toml` (`deployment/host-mcp/host-policy.example.toml`), names the tools identity and its groups, the owner, the managed units with their verbs, per-action polkit grants with their identities, the systemd tools that exist for Atlas, and the diagnostics allow list. `deployment/host-mcp/atlas_host_policy.py` renders it into `/etc/polkit-1/rules.d/50-atlas-tools.rules`, `/etc/atlas-v5/config/atlas-mcp-shell.env` and `/etc/atlas-v5/config/atlas-mcp-systemd.env`. The renderer validates shapes and prints a note for every choice that carries real risk (unit-file verbs, which polkit can only grant for all units; risky shell commands, which need `allow_risky_commands = true`; the docker group; `get_file`). It holds no list of things the owner may not do.
- **Identity:** `atlas-tools`, a system user with no home and no login, in the groups the policy lists (default `systemd-journal`). By default it cannot read `/etc/atlas-v5/secrets`, `/var/lib/atlas-v5` or the owner's home.
- **Transport:** `atlas-mcp-systemd.socket` and `atlas-mcp-shell.socket` (`root:atlas-v5 0660`, `Accept=yes`) spawn one hardened `@.service` instance per connection with stdin and stdout on the socket. Atlas reaches them because it is in the `atlas-v5` group; nothing else on the host can.
- **Editing from Control:** the Host policy card shows the effective policy and server configuration, validates edits with the same renderer the host uses (errors, notes, and the rendered polkit rule), and applies them. The runtime cannot install anything: it stages the text under `/var/lib/atlas-v5/control/host` and writes an apply request; the root-owned `atlas-host-policy.path` unit runs `apply-host-policy.sh`, which validates again, installs the files, re-renders the envelope through `apply-host-envelope.sh` (rule, environments, groups, socket restart), restores the previous policy if the envelope fails, and writes a result the card shows. A server configuration change asks for a runtime restart. The staging write exists only behind the owner-authenticated Control API and is never a capability.
- **Install:** `deployment/install-host-mcp.sh` (called by `deploy-host.sh` before the runtime starts) creates the identity, installs the pinned `systemd-mcp` binary after SHA256 verification and a pinned `mcp-shell-server` in its own tool environment, installs the units and the apply path unit, applies the envelope from the policy, and probes both sockets as the runtime identity.

`systemd-mcp` asks polkit for `com.suse.gatekeeper.readlog` on every read-only tool and for `org.freedesktop.systemd1.manage-units` (with unit and verb) on every state change. The installer declares the read action (`deployment/host-mcp/com.suse.gatekeeper.policy`); whether it is granted, and to whom, is a `[[polkit.grants]]` entry in the owner's policy (the example grants it to `atlas-tools` only). Without both, every read tool fails with "action is not registered" or "not authorized".

Polkit governs the system manager only, so an owner service Atlas should manage must be a system unit running as the owner. `deployment/host/install-owner-units.sh` converts listed `systemctl --user` units.

### 5. Verification and closure

- Every execute-effect call has a durable action row. A tool that ran and reported failure (`isError`) ends `failed` with its output; a connection that could not be opened ends `failed` before dispatch; a transport lost after the request was sent ends `uncertain` and is reconciled, never replayed.
- The model verifies with read tools (`check_restart_reload`, `list_loaded_units`, `list_log`, shell diagnostics), and the unit health monitor resolves the thread on its own when the unit is active again. The model reports through `notifications.emit`; event runs leave `last_status` and `last_result` on the schedule.
- Every action is visible: the action ledger and evidence, one journal entry per `atlas-mcp-*@` instance, the shell server's audit log, and polkit's own log of grants and denials.

## Proving case: the Desktop Commander connector

The connector's journal proved that every restart replayed a consumed refresh token (`Invalid Refresh Token: Already Used`), forcing a device code every ~17 minutes until the owner entered one. `deployment/host/desktop-commander/` carries the fix: a patch that persists the session on every token refresh and before shutdown, and `apply.sh` (owner, dry-run, backups, version pinned to 0.2.48). `deployment/host/units/` carries the system units with a start limit. The monitor stays read-only; action comes from an event schedule such as:

- filter `{"source": ["runtime.units", "runtime.rdc"], "severity": ["warning", "action_required", "critical"]}`
- prompt: "You look after the services on ubuntuserver. When the connector needs a device code, do not restart it; make sure I have the code. When a unit is failed or missing without an auth failure, inspect its log, restart it once, verify it is back, and tell me the outcome."

Acceptance proves the architecture, not the service: a stopped unit produces a monitor warning, the event run inspects and restarts through polkit-scoped authority, verifies, the monitor resolves the thread, and the owner is told. A `stop` lands in Needs You; a restart of an unlisted unit is refused by polkit; secrets are unreadable from the shell server.

## Operating notes

- Adding a governed server later is a TOML entry; adding a unit, verb, group, tool or command is a policy entry plus a deploy. No Python.
- Every default in the example files is a default. The owner's policy, the server configuration and the authority editor, all editable in Control, are the decisions.
- `atlas_mcp_probe.py <socket> [tool json]` exercises a socket by hand as any member of `atlas-v5`.
- The runtime evaluates socket availability at startup; restart `atlas-v5.service` after installing or changing servers.
- Docker control is not configured by default because the docker group is root-equivalent; listing `docker` under `identity.groups` and a docker MCP server in `mcp-servers.toml` are owner choices.
- Desktop Commander Remote MCP is not part of host control: Atlas's host access must not depend on the thing it may be asked to repair.
