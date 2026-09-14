# Atlas V5 host operations

Atlas host administration is owner-governed. The owner chooses what Atlas may do; the model does not define the operating policy.

## Authority model

Control presents host operations in operational terms. Each operation has one of three owner settings:

- **Automatic** - Atlas may execute it when needed to complete delegated work.
- **Ask me** - Atlas prepares the exact action and waits for owner approval.
- **Deny** - Atlas cannot dispatch the operation.

The setting is stored in `operation_authority` and is rechecked by the capability runtime before dispatch. It overrides tool/provider defaults. Risk descriptions inform the owner; they do not silently change authority.

| Area | Operation | Default |
| --- | --- | --- |
| System services | Inspect services | Automatic |
| System services | Read service logs | Automatic |
| System services | Start service | Automatic |
| System services | Restart service | Automatic |
| System services | Stop service | Ask me |
| System services | Enable/disable service | Ask me |
| Docker | Inspect containers | Automatic |
| Docker | Start/restart container | Automatic |
| Docker | Stop container | Ask me |
| Docker | Create/remove container | Ask me || Filesystem | Read | Automatic + owner paths |
| Filesystem | Write | Automatic + owner paths |
| Filesystem | Delete | Ask me + owner paths |
| Packages | Inspect | Automatic |
| Packages | Install/update/remove | Ask me |
| Host | View resources | Automatic |
| Host | Restart | Ask me |
| Host | Shutdown | Ask me |

These are defaults, not provider policy. The owner may change any row in Control.

## Execution boundary

Privileged execution is performed by a local, root-owned structured MCP broker on `/run/atlas-v5/mcp/host-operations.sock`. Atlas itself continues to run as the unprivileged `atlas-v5` account.

The broker exposes named operations only. It does not expose an arbitrary shell or accept shell command strings. Operations are translated to fixed argv forms for systemd, Docker, apt/dpkg and host inspection. Filesystem operations are additionally constrained to the owner-configured absolute Read / Write / Delete paths stored in `/var/lib/atlas-v5/control/host-scopes.json`.

The broker deliberately contains no authority engine. Atlas Control and the capability runtime own the authority decision and approval ledger.

## MCP surface

`host.operations` exposes:

- `services_inspect`, `service_logs`, `service_start`, `service_restart`, `service_stop`, `service_enable_disable`
- `docker_inspect`, `docker_start_restart`, `docker_stop`, `docker_create_remove`
- `filesystem_read`, `filesystem_write`, `filesystem_delete`
- `packages_inspect`, `packages_change`
- `host_resources`, `host_restart`, `host_shutdown`
Each tool becomes `host.operations.<tool>`, so restart versus stop and inspection versus mutation have independent owner authority.

## Filesystem scopes

Filesystem authority has two independent parts: operation authority and path scope. A permitted filesystem operation still fails when its target is outside the corresponding owner-configured paths. Empty lists fail closed. Control accepts absolute paths only.

The broker resolves targets before checking them against configured roots, preventing simple `..` and existing-symlink escapes. Writes use a temporary file in the target directory and atomic replace.

## Deployment and migration

`deployment/install-host-mcp.sh` installs the structured broker and socket unit, replaces only Atlas host MCP blocks while preserving unrelated/custom server definitions, and configures `ATLAS_MCP_SERVERS_FILE`.

The previous `host.systemd` / `host.shell` sockets and `atlas-host-policy.path` are disabled, and `/etc/polkit-1/rules.d/50-atlas-tools.rules` is removed. Historical policy files may remain for audit history but are not consulted by this execution path.

The live authority path is:

`Owner Control -> operation_authority / filesystem scopes -> capability dispatch and approval ledger -> structured root broker -> host`

## Verification

Before deployment, test the broker surface, fixed argv behavior, filesystem fail-closed behavior, operation-authority precedence, MCP registration, installer migration, frontend controls and production build. After deployment, verify the broker socket answers as `atlas-v5`, old host-policy sockets are inactive, the new capability appears in Control, and a harmless read operation succeeds end to end.
