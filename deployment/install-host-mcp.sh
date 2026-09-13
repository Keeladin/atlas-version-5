#!/usr/bin/env bash
# Install the governed host MCP servers for Atlas V5: the tools identity, the openSUSE
# systemd-mcp binary, tumf's mcp-shell-server, the envelope rendered from the owner's policy,
# and the socket-activated units. Idempotent; run as root. The policy decides what is granted.
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run this script with sudo/root." >&2
  exit 1
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TOOLS_USER=atlas-tools
APP_GROUP=atlas-v5
BIN_DIR=/opt/atlas-v5/bin
TOOLS_DIR=/opt/atlas-v5/tools
CONFIG_DIR=/etc/atlas-v5/config
POLICY=${CONFIG_DIR}/host-policy.toml
MCP_SERVERS=${CONFIG_DIR}/mcp-servers.toml
SHELL_ENV=${CONFIG_DIR}/atlas-mcp-shell.env
SYSTEMD_ENV=${CONFIG_DIR}/atlas-mcp-systemd.env
POLKIT_RULE=/etc/polkit-1/rules.d/50-atlas-tools.rules
RUNTIME_ENV=${CONFIG_DIR}/runtime.env
UV_BIN=/home/jaco/.local/bin/uv
PYTHON_INSTALL_DIR=/opt/atlas-v5/python

SYSTEMD_MCP_VERSION=v0.3.4
SYSTEMD_MCP_URL="https://github.com/openSUSE/systemd-mcp/releases/download/${SYSTEMD_MCP_VERSION}/systemd-mcp"
SYSTEMD_MCP_SHA256=b9332d93c02b451c0db55f98c09193ee3b6fc303d631f60a9dee302a9ef8d8d4
MCP_SHELL_SERVER_VERSION=1.1.9

[[ -f ${RUNTIME_ENV} ]] || { echo "Atlas runtime env not found: ${RUNTIME_ENV}" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "python3 is required." >&2; exit 1; }
command -v pkaction >/dev/null 2>&1 || { echo "polkit is required (pkaction not found)." >&2; exit 1; }
command -v setfacl >/dev/null 2>&1 || { echo "setfacl is required." >&2; exit 1; }
[[ -x ${UV_BIN} ]] || { echo "uv was not found at ${UV_BIN}." >&2; exit 1; }

install -d -o root -g "${APP_GROUP}" -m 0750 "${BIN_DIR}" "${TOOLS_DIR}" "${CONFIG_DIR}"

# 1. Owner policy and server configuration: installed once, never overwritten.
if [[ ! -f ${POLICY} ]]; then
  install -o root -g "${APP_GROUP}" -m 0640 "${ROOT_DIR}/deployment/host-mcp/host-policy.example.toml" "${POLICY}"
  echo "Installed the example host policy at ${POLICY}; edit it to decide what Atlas may do on this host."
fi
if [[ ! -f ${MCP_SERVERS} ]]; then
  install -o root -g "${APP_GROUP}" -m 0640 "${ROOT_DIR}/deployment/mcp-servers.example.toml" "${MCP_SERVERS}"
  echo "Installed the example MCP server configuration at ${MCP_SERVERS}."
fi

# 2. Renderer, probe, and the scripts that make a policy effective (shared with the Control apply unit).
install -o root -g "${APP_GROUP}" -m 0750 "${ROOT_DIR}/deployment/host-mcp/atlas_host_policy.py" "${BIN_DIR}/atlas_host_policy.py"
install -o root -g "${APP_GROUP}" -m 0750 "${ROOT_DIR}/deployment/host-mcp/atlas_mcp_probe.py" "${BIN_DIR}/atlas_mcp_probe.py"
install -o root -g root -m 0750 "${ROOT_DIR}/deployment/host-mcp/apply-host-envelope.sh" "${BIN_DIR}/apply-host-envelope.sh"
install -o root -g root -m 0750 "${ROOT_DIR}/deployment/host-mcp/apply-host-policy.sh" "${BIN_DIR}/apply-host-policy.sh"
python3 "${BIN_DIR}/atlas_host_policy.py" --policy "${POLICY}" --check
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# 3. The tools identity exists before the envelope is applied; groups come from the policy.
if ! id "${TOOLS_USER}" >/dev/null 2>&1; then
  useradd --system --user-group --home-dir /nonexistent --shell /usr/sbin/nologin "${TOOLS_USER}"
fi

# atlas-tools executes binaries below /opt/atlas-v5 but must not gain directory listing
# or read access to the Atlas application/runtime tree. Grant traverse-only ACLs on the
# shared path components; individual executables/environments remain separately scoped.
setfacl -m "u:${TOOLS_USER}:--x" /opt/atlas-v5 "${BIN_DIR}" "${TOOLS_DIR}"

install -d -o root -g root -m 0755 /etc/polkit-1/rules.d /usr/share/polkit-1/actions
# systemd-mcp asks polkit for com.suse.gatekeeper.readlog on every read; polkit refuses
# actions nobody declared, so the declaration ships with Atlas (the owner's policy grants it).
install -o root -g root -m 0644 "${ROOT_DIR}/deployment/host-mcp/com.suse.gatekeeper.policy" /usr/share/polkit-1/actions/com.suse.gatekeeper.policy

# 4. openSUSE systemd-mcp: pinned release, verified by SHA256.
if [[ ! -f ${BIN_DIR}/systemd-mcp ]] || ! echo "${SYSTEMD_MCP_SHA256}  ${BIN_DIR}/systemd-mcp" | sha256sum -c --status; then
  curl --fail --silent --show-error --location "${SYSTEMD_MCP_URL}" -o "${TMP}/systemd-mcp"
  echo "${SYSTEMD_MCP_SHA256}  ${TMP}/systemd-mcp" | sha256sum -c --status
  install -o root -g "${TOOLS_USER}" -m 0750 "${TMP}/systemd-mcp" "${BIN_DIR}/systemd-mcp"
fi

# 5. tumf/mcp-shell-server: pinned, in its own tool environment under /opt/atlas-v5/tools.
export UV_TOOL_DIR="${TOOLS_DIR}"
export UV_TOOL_BIN_DIR="${TOOLS_DIR}/bin"
export UV_PYTHON_INSTALL_DIR="${PYTHON_INSTALL_DIR}"
# uv normally hardlinks packages from its cache. The tool tree is later chowned to
# atlas-tools, so hardlinks could change ownership of the same inode inside Atlas
# runtime venv. Force copies to keep the execution domains ownership-isolated.
export UV_LINK_MODE=copy
"${UV_BIN}" tool install --quiet --python 3.14 --force "mcp-shell-server==${MCP_SHELL_SERVER_VERSION}"
[[ -x ${TOOLS_DIR}/mcp-shell-server/bin/mcp-shell-server ]] || { echo "mcp-shell-server did not install where the unit expects it." >&2; exit 1; }
chown -R root:"${TOOLS_USER}" "${TOOLS_DIR}/mcp-shell-server"
find "${TOOLS_DIR}/mcp-shell-server" -type d -exec chmod 0750 {} +
find "${TOOLS_DIR}/mcp-shell-server" -type f -exec chmod u=rw,g=r,o= {} +
find "${TOOLS_DIR}/mcp-shell-server/bin" -type f -exec chmod u=rwx,g=rx,o= {} +
# uv's tool entry point ultimately execs the managed Python under /opt/atlas-v5/python.
# atlas-tools is intentionally not in atlas-v5, so grant read/execute only on that runtime.
setfacl -Rm "u:${TOOLS_USER}:rX" "${PYTHON_INSTALL_DIR}"

# 6. Socket-activated units, then the envelope from the policy (rule, environments, groups, socket restart).
for unit in atlas-mcp-systemd.socket atlas-mcp-systemd@.service atlas-mcp-shell.socket atlas-mcp-shell@.service \
    atlas-host-policy.path atlas-host-policy-apply.service; do
  install -o root -g root -m 0644 "${ROOT_DIR}/deployment/systemd/${unit}" "/etc/systemd/system/${unit}"
done
systemctl daemon-reload
systemctl enable --now atlas-mcp-systemd.socket atlas-mcp-shell.socket
bash "${BIN_DIR}/apply-host-envelope.sh"

# 6b. Edits made in Control are staged here by the runtime and applied by the root path unit.
install -d -o "${APP_GROUP}" -g "${APP_GROUP}" -m 0700 /var/lib/atlas-v5/control /var/lib/atlas-v5/control/host
systemctl enable --now atlas-host-policy.path

# 7. Runtime settings: the servers file, and the managed units as the watch list unless the owner set one.
set_env() {
  local key=$1 value=$2
  if grep -q "^${key}=" "${RUNTIME_ENV}"; then
    sed -i "s|^${key}=.*|${key}=${value}|" "${RUNTIME_ENV}"
  else
    printf '%s=%s\n' "${key}" "${value}" >> "${RUNTIME_ENV}"
  fi
}
set_env ATLAS_MCP_SERVERS_FILE "${MCP_SERVERS}"
set_env ATLAS_HOST_POLICY_FILE "${POLICY}"
set_env ATLAS_HOST_POLICY_RENDERER "${BIN_DIR}/atlas_host_policy.py"
if ! grep -q '^ATLAS_HOST_WATCH_UNITS=' "${RUNTIME_ENV}"; then
  set_env ATLAS_HOST_WATCH_UNITS "$(python3 "${BIN_DIR}/atlas_host_policy.py" --policy "${POLICY}" --print-units)"
fi
chown root:"${APP_GROUP}" "${RUNTIME_ENV}"
chmod 0640 "${RUNTIME_ENV}"

# 8. Smoke test as the runtime identity: handshake and tool list through each socket.
for sock in systemd shell; do
  if ! runuser -u atlas-v5 -- /usr/bin/python3 "${BIN_DIR}/atlas_mcp_probe.py" "/run/atlas-v5/mcp/${sock}.sock"; then
    echo "The ${sock} MCP socket did not answer a tools/list; check 'journalctl -u atlas-mcp-${sock}@*'." >&2
    exit 1
  fi
done
echo "Governed host MCP servers are installed. The host policy and server configuration are editable in Control; enable the servers and set per-operation authority there, then restart atlas-v5.service if it was already running."
