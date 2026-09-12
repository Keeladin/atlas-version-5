#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run this script with sudo/root." >&2
  exit 1
fi

APP_ROOT=/opt/atlas-v5
OBSERVER_ROOT=/var/lib/atlas-v5/observer
MAINTAINER=jaco

if ! id "${MAINTAINER}" >/dev/null 2>&1; then
  echo "Maintenance user is missing: ${MAINTAINER}" >&2
  exit 1
fi
if ! command -v setfacl >/dev/null 2>&1; then
  echo "setfacl is required for Atlas maintenance access." >&2
  exit 1
fi
if [[ ! -d ${APP_ROOT} ]]; then
  echo "Atlas deployment root is missing: ${APP_ROOT}" >&2
  exit 1
fi
if [[ ! -d ${OBSERVER_ROOT} ]]; then
  echo "Atlas observer root is missing: ${OBSERVER_ROOT}" >&2
  exit 1
fi

# Read/traverse only. Secrets live under /etc/atlas-v5 and are deliberately excluded.
find "${APP_ROOT}" -type d -exec setfacl -n -m u:${MAINTAINER}:r-x,m::r-x {} +
find "${APP_ROOT}" -type f -perm /111 -exec setfacl -n -m u:${MAINTAINER}:r-x,m::r-x {} +
find "${APP_ROOT}" -type f ! -perm /111 -exec setfacl -n -m u:${MAINTAINER}:r--,m::r-- {} +

# Observer state is separate from protected runtime control/auth state.
setfacl -n -m u:${MAINTAINER}:r-x,m::r-x "${OBSERVER_ROOT}"
setfacl -n -d -m u:${MAINTAINER}:r-x,m::r-x "${OBSERVER_ROOT}"
find "${OBSERVER_ROOT}" -maxdepth 1 -type f -exec setfacl -n -m u:${MAINTAINER}:r--,m::r-- {} +

printf 'Read-only maintenance access granted for %s on %s and %s\n' \
  "${MAINTAINER}" "${APP_ROOT}" "${OBSERVER_ROOT}"
