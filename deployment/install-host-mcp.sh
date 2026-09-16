#!/usr/bin/env bash
# Install Atlas V5 structured host operations plus the owner-side coding worker boundary.
# Owner authority is stored and enforced by Atlas Control. Brokers expose structured
# capabilities only; none of them is an independent permission engine.
set -euo pipefail
[[ ${EUID} -eq 0 ]] || { echo "Run this script with sudo/root." >&2; exit 1; }

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_GROUP=atlas-v5
BIN_DIR=/opt/atlas-v5/bin
CONFIG_DIR=/etc/atlas-v5/config
MCP_SERVERS=${CONFIG_DIR}/mcp-servers.toml
RUNTIME_ENV=${CONFIG_DIR}/runtime.env
SCOPES=/var/lib/atlas-v5/control/host-scopes.json
CODEX_BIN=/home/jaco/.local/node/bin/codex
CODING_STATE=/home/jaco/.local/state/atlas-v5/coding

[[ -f ${RUNTIME_ENV} ]] || { echo "Atlas runtime env not found: ${RUNTIME_ENV}" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "python3 is required." >&2; exit 1; }
[[ -x ${CODEX_BIN} ]] || { echo "Codex CLI not found or not executable: ${CODEX_BIN}" >&2; exit 1; }

install -d -o root -g "${APP_GROUP}" -m 0750 "${BIN_DIR}" "${CONFIG_DIR}"
install -d -o "${APP_GROUP}" -g "${APP_GROUP}" -m 0700 /var/lib/atlas-v5/control
install -d -o jaco -g jaco -m 0700 "${CODING_STATE}"
install -o root -g root -m 0755 "${ROOT_DIR}/deployment/host-mcp/atlas_host_operations_server.py" "${BIN_DIR}/atlas_host_operations_server.py"
install -o root -g "${APP_GROUP}" -m 0750 "${ROOT_DIR}/deployment/host-mcp/atlas_mcp_probe.py" "${BIN_DIR}/atlas_mcp_probe.py"
if [[ ! -f ${MCP_SERVERS} ]]; then
  install -o root -g "${APP_GROUP}" -m 0640 "${ROOT_DIR}/deployment/mcp-servers.example.toml" "${MCP_SERVERS}"
else
  # Replace Atlas-managed server blocks while preserving unrelated/custom MCP servers.
  python3 - "${MCP_SERVERS}" "${ROOT_DIR}/deployment/mcp-servers.example.toml" <<'PY_MCP'
import re, sys
from pathlib import Path
current, managed = map(Path, sys.argv[1:])
text = current.read_text()
parts = re.split(r'(?m)(?=^\[\[servers\]\]\s*$)', text)
managed_ids = {
    "host.systemd", "host.shell", "host.operations",
    "coding.agent", "workspace.tasks",
}
kept = []
for part in parts:
    match = re.search(r'(?m)^id\s*=\s*"([^"]+)"\s*$', part)
    if match and match.group(1) in managed_ids:
        continue
    if part.strip():
        kept.append(part.rstrip())
merged = "\n\n".join(kept + [managed.read_text().rstrip()]) + "\n"
tmp = current.with_suffix('.tmp')
tmp.write_text(merged)
tmp.chmod(0o640)
tmp.replace(current)
PY_MCP
  chown root:"${APP_GROUP}" "${MCP_SERVERS}"
fi

if [[ ! -f ${SCOPES} ]]; then
  printf '{"read":[],"write":[],"delete":[]}\n' > "${SCOPES}"
  chown "${APP_GROUP}:${APP_GROUP}" "${SCOPES}"
  chmod 0600 "${SCOPES}"
fi

for unit in \
  atlas-host-operations.socket atlas-host-operations@.service \
  atlas-coding-agent.socket atlas-coding-agent.service \
  atlas-v5-managed-tasks.service; do
  install -o root -g root -m 0644 "${ROOT_DIR}/deployment/systemd/${unit}" "/etc/systemd/system/${unit}"
done

# Retire the old duplicated host-policy/polkit path. Existing source files are left in /etc
# for audit/history, but they are not consulted by the new broker.
systemctl disable --now atlas-host-policy.path atlas-mcp-systemd.socket atlas-mcp-shell.socket 2>/dev/null || true
rm -f /etc/polkit-1/rules.d/50-atlas-tools.rules
systemctl daemon-reload
systemctl enable --now atlas-host-operations.socket
systemctl enable --now atlas-coding-agent.socket
# Do not start the managed worker here: deploy-host invokes this script while the main
# Atlas service is intentionally stopped. WantedBy=atlas-v5.service starts it with Atlas.
systemctl enable atlas-v5-managed-tasks.service

set_env() {
  local key=$1 value=$2
  if grep -q "^${key}=" "${RUNTIME_ENV}"; then sed -i "s|^${key}=.*|${key}=${value}|" "${RUNTIME_ENV}"
  else printf '%s=%s\n' "${key}" "${value}" >> "${RUNTIME_ENV}"; fi
}
unset_env() { sed -i "/^$1=/d" "${RUNTIME_ENV}"; }
set_env ATLAS_MCP_SERVERS_FILE "${MCP_SERVERS}"
unset_env ATLAS_HOST_POLICY_FILE
unset_env ATLAS_HOST_POLICY_RENDERER
chown root:"${APP_GROUP}" "${RUNTIME_ENV}"
chmod 0640 "${RUNTIME_ENV}"

# Smoke-test both sockets through the same atlas-v5 identity the runtime uses.
if ! runuser -u atlas-v5 -- /usr/bin/python3 "${BIN_DIR}/atlas_mcp_probe.py" "/run/atlas-v5/mcp/host-operations.sock"; then
  echo "Host operations MCP socket did not answer; check journalctl -u 'atlas-host-operations@*'." >&2
  exit 1
fi
if ! runuser -u atlas-v5 -- /usr/bin/python3 "${BIN_DIR}/atlas_mcp_probe.py" "/run/atlas-v5/mcp/coding-agent.sock"; then
  echo "Coding agent MCP socket did not answer; check journalctl -u atlas-coding-agent.service." >&2
  exit 1
fi

echo "Atlas structured host operations and owner-side coding worker are installed. Authority is controlled in Atlas Control; polkit is not part of this execution path."
