#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run this script with sudo/root." >&2
  exit 1
fi

GWS_SOURCE=${1:-/home/jaco/Projects/atlas-agent-state/production/bin/gws}
CONFIG_SOURCE=${2:-/home/jaco/Projects/atlas-agent-state/production/google-workspace/config}
RUNTIME_ENV=/etc/atlas-v5/config/runtime.env
GWS_TARGET=/opt/atlas-v5/bin/gws
STATE_ROOT=/var/lib/atlas-v5/google-workspace
CONFIG_TARGET=${STATE_ROOT}/config

[[ -x ${GWS_SOURCE} ]] || { echo "gws executable not found: ${GWS_SOURCE}" >&2; exit 1; }
[[ -f ${CONFIG_SOURCE}/client_secret.json ]] || { echo "Google OAuth client config not found" >&2; exit 1; }
[[ -f ${CONFIG_SOURCE}/credentials.enc ]] || { echo "Google encrypted credentials not found" >&2; exit 1; }
[[ -f ${CONFIG_SOURCE}/.encryption_key ]] || { echo "Google credential encryption key not found" >&2; exit 1; }
[[ -f ${RUNTIME_ENV} ]] || { echo "Atlas runtime env not found: ${RUNTIME_ENV}" >&2; exit 1; }

install -d -o root -g atlas-v5 -m 0750 /opt/atlas-v5/bin
install -d -o atlas-v5 -g atlas-v5 -m 0750 "${CONFIG_TARGET}" "${STATE_ROOT}/workspace"
install -o root -g atlas-v5 -m 0750 "${GWS_SOURCE}" "${GWS_TARGET}"
for name in client_secret.json client_secret.web.json credentials.enc .encryption_key token_cache.json; do
  if [[ -f ${CONFIG_SOURCE}/${name} ]]; then
    install -o atlas-v5 -g atlas-v5 -m 0600 "${CONFIG_SOURCE}/${name}" "${CONFIG_TARGET}/${name}"
  fi
done
rm -f /etc/atlas-v5/secrets/google-workspace-authorized-user.json

set_env() {
  local key=$1 value=$2
  if grep -q "^${key}=" "${RUNTIME_ENV}"; then
    sed -i "s|^${key}=.*|${key}=${value}|" "${RUNTIME_ENV}"
  else
    printf '%s=%s\n' "${key}" "${value}" >> "${RUNTIME_ENV}"
  fi
}

set_env ATLAS_GWS_COMMAND "${GWS_TARGET}"
sed -i '/^ATLAS_GWS_CREDENTIALS_FILE=/d' "${RUNTIME_ENV}"
set_env ATLAS_GWS_CONFIG_DIR "${CONFIG_TARGET}"
set_env ATLAS_GWS_WORKSPACE_DIR "${STATE_ROOT}/workspace"

chown root:atlas-v5 "${RUNTIME_ENV}"
chmod 0640 "${RUNTIME_ENV}"
echo "Google Workspace OAuth configuration provisioned for Atlas V5."
echo "OAuth secrets remain hidden; Atlas-side authority still controls mutations."
