#!/usr/bin/env bash
# Render the owner's host policy into the OS envelope and make it effective: polkit rule,
# server environments, tools identity groups, socket restart. Root only. Shared by
# install-host-mcp.sh and the apply unit that serves edits made in Control.
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run this script with sudo/root." >&2
  exit 1
fi

TOOLS_USER=atlas-tools
APP_GROUP=atlas-v5
BIN_DIR=/opt/atlas-v5/bin
CONFIG_DIR=/etc/atlas-v5/config
POLICY=${CONFIG_DIR}/host-policy.toml
SHELL_ENV=${CONFIG_DIR}/atlas-mcp-shell.env
SYSTEMD_ENV=${CONFIG_DIR}/atlas-mcp-systemd.env
POLKIT_RULE=/etc/polkit-1/rules.d/50-atlas-tools.rules
RUNTIME_ENV=${CONFIG_DIR}/runtime.env

[[ -f ${POLICY} ]] || { echo "Host policy not found: ${POLICY}" >&2; exit 1; }
[[ -f ${BIN_DIR}/atlas_host_policy.py ]] || { echo "Renderer not installed: ${BIN_DIR}/atlas_host_policy.py" >&2; exit 1; }

python3 "${BIN_DIR}/atlas_host_policy.py" --policy "${POLICY}" --check
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
python3 "${BIN_DIR}/atlas_host_policy.py" --policy "${POLICY}" \
  --polkit-out "${TMP}/50-atlas-tools.rules" --shell-env-out "${TMP}/atlas-mcp-shell.env" --systemd-env-out "${TMP}/atlas-mcp-systemd.env"

if ! id "${TOOLS_USER}" >/dev/null 2>&1; then
  useradd --system --user-group --home-dir /nonexistent --shell /usr/sbin/nologin "${TOOLS_USER}"
fi
POLICY_GROUPS="$(python3 "${BIN_DIR}/atlas_host_policy.py" --policy "${POLICY}" --print-groups)"
IFS=',' read -r -a groups <<< "${POLICY_GROUPS}"
for group in "${groups[@]}"; do
  [[ -n ${group} ]] || continue
  getent group "${group}" >/dev/null 2>&1 || { echo "Policy names group ${group}, which does not exist on this host." >&2; exit 1; }
  if ! id -nG "${TOOLS_USER}" | grep -qw "${group}"; then
    usermod -aG "${group}" "${TOOLS_USER}"
  fi
done
# Groups the policy no longer lists are removed, so the policy is the whole grant.
for group in $(id -nG "${TOOLS_USER}"); do
  [[ ${group} == "${TOOLS_USER}" ]] && continue
  if ! grep -qw "${group}" <<< "${POLICY_GROUPS}"; then
    gpasswd -d "${TOOLS_USER}" "${group}" >/dev/null
    echo "Removed ${TOOLS_USER} from ${group} (not in the policy)."
  fi
done

install -d -o root -g root -m 0755 /etc/polkit-1/rules.d
install -o root -g root -m 0644 "${TMP}/50-atlas-tools.rules" "${POLKIT_RULE}"
install -o root -g "${TOOLS_USER}" -m 0640 "${TMP}/atlas-mcp-shell.env" "${SHELL_ENV}"
install -o root -g "${TOOLS_USER}" -m 0640 "${TMP}/atlas-mcp-systemd.env" "${SYSTEMD_ENV}"

if [[ -f ${RUNTIME_ENV} ]] && ! grep -q '^ATLAS_HOST_WATCH_UNITS=' "${RUNTIME_ENV}"; then
  printf 'ATLAS_HOST_WATCH_UNITS=%s\n' "$(python3 "${BIN_DIR}/atlas_host_policy.py" --policy "${POLICY}" --print-units)" >> "${RUNTIME_ENV}"
  chown root:"${APP_GROUP}" "${RUNTIME_ENV}"
  chmod 0640 "${RUNTIME_ENV}"
fi

for socket in atlas-mcp-systemd.socket atlas-mcp-shell.socket; do
  if systemctl cat "${socket}" >/dev/null 2>&1; then
    systemctl restart "${socket}"
  fi
done
echo "Host envelope applied from ${POLICY}."
