#!/usr/bin/env bash
# Serve an apply request staged by Control: validate the staged files, make them effective,
# re-render the envelope, and report the outcome where the runtime can read it. Root only;
# started by atlas-host-policy.path whenever the runtime writes the request file.
set -uo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run this script with sudo/root." >&2
  exit 1
fi

APP_USER=atlas-v5
BIN_DIR=/opt/atlas-v5/bin
CONFIG_DIR=/etc/atlas-v5/config
STAGING=/var/lib/atlas-v5/control/host
REQUEST=${STAGING}/apply.request
RESULT=${STAGING}/apply.result.json
POLICY_TARGET=${CONFIG_DIR}/host-policy.toml
SERVERS_TARGET=${CONFIG_DIR}/mcp-servers.toml
VENV_PYTHON=/opt/atlas-v5/venv/bin/python
APP_BACKEND=/opt/atlas-v5/app/backend

[[ -f ${REQUEST} ]] || exit 0

applied=()
errors=()
notes=()
restart_required=false

finish() {
  local status=$1
  local applied_json errors_json notes_json
  applied_json=$(printf '%s\n' "${applied[@]:-}" | python3 -c 'import json,sys; print(json.dumps([l for l in sys.stdin.read().splitlines() if l]))')
  errors_json=$(printf '%s\n' "${errors[@]:-}" | python3 -c 'import json,sys; print(json.dumps([l for l in sys.stdin.read().splitlines() if l]))')
  notes_json=$(printf '%s\n' "${notes[@]:-}" | python3 -c 'import json,sys; print(json.dumps([l for l in sys.stdin.read().splitlines() if l]))')
  printf '{"status": "%s", "finished_at": "%s", "applied": %s, "errors": %s, "notes": %s, "restart_required": %s}\n' \
    "${status}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${applied_json}" "${errors_json}" "${notes_json}" "${restart_required}" > "${RESULT}.tmp"
  chown "${APP_USER}:${APP_USER}" "${RESULT}.tmp"
  chmod 0640 "${RESULT}.tmp"
  mv -f "${RESULT}.tmp" "${RESULT}"
  rm -f "${REQUEST}" "${STAGING}/host-policy.toml" "${STAGING}/mcp-servers.toml"
  echo "Host policy apply finished: ${status}"
}

# 1. Host policy: validate the staged text with the renderer, then make it effective.
if [[ -f ${STAGING}/host-policy.toml ]]; then
  if check=$(python3 "${BIN_DIR}/atlas_host_policy.py" --policy "${STAGING}/host-policy.toml" --check 2>&1); then
    while IFS= read -r line; do [[ ${line} == "host policy note:"* ]] && notes+=("${line#host policy note: }"); done <<< "${check}"
    if [[ -f ${POLICY_TARGET} ]]; then
      cp -p "${POLICY_TARGET}" "${POLICY_TARGET}.previous"
    fi
    install -o root -g atlas-v5 -m 0640 "${STAGING}/host-policy.toml" "${POLICY_TARGET}"
    if envelope=$(bash "${BIN_DIR}/apply-host-envelope.sh" 2>&1); then
      applied+=("host-policy.toml")
    else
      errors+=("envelope apply failed: ${envelope##*$'\n'}")
      if [[ -f ${POLICY_TARGET}.previous ]]; then
        install -o root -g atlas-v5 -m 0640 "${POLICY_TARGET}.previous" "${POLICY_TARGET}"
        bash "${BIN_DIR}/apply-host-envelope.sh" >/dev/null 2>&1 || true
        notes+=("previous host policy restored")
      fi
    fi
  else
    errors+=("host policy rejected: ${check##*$'\n'}")
  fi
fi

# 2. Governed server configuration: validated with the runtime's own parser as the runtime identity.
if [[ -f ${STAGING}/mcp-servers.toml ]]; then
  if check=$(runuser -u "${APP_USER}" -- env PYTHONPATH="${APP_BACKEND}" "${VENV_PYTHON}" -c \
      'import sys; from atlas.integrations.mcp_servers import parse_mcp_servers; print(len(parse_mcp_servers(open(sys.argv[1]).read())), "servers")' \
      "${STAGING}/mcp-servers.toml" 2>&1); then
    install -o root -g atlas-v5 -m 0640 "${STAGING}/mcp-servers.toml" "${SERVERS_TARGET}"
    applied+=("mcp-servers.toml")
    restart_required=true
    notes+=("restart atlas-v5.service to load the new server configuration")
  else
    errors+=("MCP server configuration rejected: ${check##*$'\n'}")
  fi
fi

if [[ ${#errors[@]} -gt 0 ]]; then
  finish "failed"
  exit 0
fi
finish "applied"
