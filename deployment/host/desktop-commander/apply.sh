#!/usr/bin/env bash
# Apply the session-persistence patch to the installed Desktop Commander connector.
# Run as the owner (the package lives in the owner's home). Idempotent and dry-run first.
set -euo pipefail

if [[ ${EUID} -eq 0 ]]; then
  echo "Run this as the owner, not root: the connector is installed in the owner's home." >&2
  exit 1
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG=${DESKTOP_COMMANDER_DIR:-$HOME/.local/node/lib/node_modules/@wonderwhy-er/desktop-commander}
PATCH=${HERE}/desktop-commander-0.2.48-persist-session.patch
EXPECTED_VERSION=0.2.48
FORCE=${1:-}

command -v patch >/dev/null 2>&1 || { echo "patch(1) is required." >&2; exit 1; }
command -v node >/dev/null 2>&1 || { echo "node is required." >&2; exit 1; }
[[ -f ${PKG}/package.json ]] || { echo "Desktop Commander is not installed at ${PKG}." >&2; exit 1; }

VERSION=$(node -p "require('${PKG}/package.json').version")
if [[ ${VERSION} != "${EXPECTED_VERSION}" && ${FORCE} != "--force" ]]; then
  echo "Installed version is ${VERSION}; this patch was made for ${EXPECTED_VERSION}. Re-check the code and pass --force to try anyway." >&2
  exit 1
fi

if patch -p1 -R --dry-run -s -d "${PKG}" -i "${PATCH}" >/dev/null 2>&1; then
  echo "Patch is already applied to ${PKG} (${VERSION})."
  exit 0
fi

echo "Checking that the patch applies cleanly..."
patch -p1 --dry-run -d "${PKG}" -i "${PATCH}"

BACKUP=$HOME/.desktop-commander-device/backup/atlas-${VERSION}-$(date +%Y%m%d-%H%M%S)
mkdir -p "${BACKUP}"
cp "${PKG}/dist/remote-device/device.js" "${PKG}/dist/remote-device/remote-channel.js" "${BACKUP}/"
patch -p1 -b -z .atlas-orig -d "${PKG}" -i "${PATCH}"

cat <<EOF

Patched ${PKG} (${VERSION}); originals kept as *.atlas-orig and in ${BACKUP}.
The running connector still executes the old code. Its in-memory refresh token has probably rotated since
device.json was written, so the FIRST restart after this patch will most likely ask for one device code.
Restart only when you can enter it:
  systemctl --user restart desktop-commander.service     (while it is still a user unit)
  sudo systemctl restart desktop-commander.service       (after install-owner-units.sh)
From then on every refresh is persisted and later restarts log "Session restored".
An npm update of the package replaces dist/ and removes this patch; run this script again afterwards.
EOF
