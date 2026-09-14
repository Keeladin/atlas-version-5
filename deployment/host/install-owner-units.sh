#!/usr/bin/env bash
# Move the owner's services from `systemctl --user` units to system units running as the
# owner, so polkit (and therefore Atlas host policy) can govern them. Idempotent; run as root.
#
# Stopping desktop-commander burns its persisted refresh token unless the connector patch
# (deployment/host/desktop-commander/apply.sh) is applied and a refresh has been saved since,
# so run this with the owner present and one device code entry expected.
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run this script with sudo/root." >&2
  exit 1
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OWNER=${OWNER_USER:-jaco}
OWNER_UID=$(id -u "${OWNER}")
USER_UNIT_DIR=/home/${OWNER}/.config/systemd/user
RUNTIME_ENV=/etc/atlas-v5/config/runtime.env
UNITS=(desktop-commander.service empire-control.service empire-account-portal.service)

owner_systemctl() {
  runuser -u "${OWNER}" -- env "XDG_RUNTIME_DIR=/run/user/${OWNER_UID}" "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/${OWNER_UID}/bus" systemctl --user "$@"
}

for unit in "${UNITS[@]}"; do
  if owner_systemctl cat "${unit}" >/dev/null 2>&1; then
    echo "Stopping and disabling the user unit ${unit}"
    owner_systemctl disable --now "${unit}" || true
  fi
  if [[ -f ${USER_UNIT_DIR}/${unit} ]]; then
    mv -f "${USER_UNIT_DIR}/${unit}" "${USER_UNIT_DIR}/${unit}.disabled"
    chown "${OWNER}:${OWNER}" "${USER_UNIT_DIR}/${unit}.disabled"
  fi
  if [[ -d ${USER_UNIT_DIR}/${unit}.d ]]; then
    mv -f "${USER_UNIT_DIR}/${unit}.d" "${USER_UNIT_DIR}/${unit}.d.disabled"
  fi
  install -o root -g root -m 0644 "${ROOT_DIR}/deployment/host/units/${unit}" "/etc/systemd/system/${unit}"
done
owner_systemctl daemon-reload || true
systemctl daemon-reload
for unit in "${UNITS[@]}"; do
  systemctl enable --now "${unit}"
done

if [[ -f ${RUNTIME_ENV} ]]; then
  if grep -q '^ATLAS_RDC_MONITOR_SCOPE=' "${RUNTIME_ENV}"; then
    sed -i 's|^ATLAS_RDC_MONITOR_SCOPE=.*|ATLAS_RDC_MONITOR_SCOPE=system|' "${RUNTIME_ENV}"
  else
    echo 'ATLAS_RDC_MONITOR_SCOPE=system' >> "${RUNTIME_ENV}"
  fi
fi

systemctl --no-pager --no-legend list-units "${UNITS[@]}" || true
cat <<EOF

Owner units now run as system units (User=${OWNER}). The former user unit files were renamed *.disabled.
Restart atlas-v5.service so the connector monitor follows the unit into the system journal
(ATLAS_RDC_MONITOR_SCOPE=system). If desktop-commander asks for a device code, Atlas will tell you.
EOF
