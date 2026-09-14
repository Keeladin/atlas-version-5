#!/usr/bin/env bash
# Provision the VAPID signing key for Atlas Web Push. Idempotent: an existing key is kept.
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run this script with sudo/root." >&2
  exit 1
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_ENV=/etc/atlas-v5/config/runtime.env
KEY_TARGET=/etc/atlas-v5/secrets/push-vapid-private-key
SUBJECT=${ATLAS_PUSH_VAPID_SUBJECT:-}

[[ -f ${RUNTIME_ENV} ]] || { echo "Atlas runtime env not found: ${RUNTIME_ENV}" >&2; exit 1; }
command -v openssl >/dev/null 2>&1 || { echo "openssl is required." >&2; exit 1; }

install -d -o root -g atlas-v5 -m 0750 /etc/atlas-v5/secrets
if [[ ! -s ${KEY_TARGET} ]]; then
  TMP=$(mktemp)
  trap 'rm -f "$TMP"' EXIT
  openssl ecparam -name prime256v1 -genkey -noout | openssl pkcs8 -topk8 -nocrypt -out "${TMP}"
  install -o root -g atlas-v5 -m 0640 "${TMP}" "${KEY_TARGET}"
  echo "Generated a new VAPID key at ${KEY_TARGET}."
else
  echo "Keeping the existing VAPID key at ${KEY_TARGET}."
fi

set_env() {
  local key=$1 value=$2
  if grep -q "^${key}=" "${RUNTIME_ENV}"; then
    sed -i "s|^${key}=.*|${key}=${value}|" "${RUNTIME_ENV}"
  else
    printf '%s=%s\n' "${key}" "${value}" >> "${RUNTIME_ENV}"
  fi
}

set_env ATLAS_PUSH_VAPID_PRIVATE_KEY_FILE "${KEY_TARGET}"
if [[ -n ${SUBJECT} ]]; then
  set_env ATLAS_PUSH_VAPID_SUBJECT "${SUBJECT}"
elif ! grep -q '^ATLAS_PUSH_VAPID_SUBJECT=' "${RUNTIME_ENV}"; then
  set_env ATLAS_PUSH_VAPID_SUBJECT "mailto:owner@localhost"
fi
chown root:atlas-v5 "${RUNTIME_ENV}"
chmod 0640 "${RUNTIME_ENV}"
bash "${ROOT_DIR}/deployment/grant-maintenance-access.sh"

public_key=$(openssl ec -in "${KEY_TARGET}" -pubout -outform DER 2>/dev/null | tail -c 65 | base64 -w0 | tr '+/' '-_' | tr -d '=')
echo "VAPID public key: ${public_key}"
echo "Set ATLAS_PUSH_VAPID_SUBJECT to a mailto: address you own (ATLAS_PUSH_VAPID_SUBJECT=mailto:you@example.com sudo $0), then restart atlas-v5.service."
