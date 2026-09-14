#!/usr/bin/env bash
set -euo pipefail

GWS=${GWS_COMMAND:-/opt/atlas-v5/bin/gws}
CONFIG_DIR=${GWS_CONFIG_DIR:-/var/lib/atlas-v5/control/google-workspace-config}
SCOPES=${GWS_SCOPES:-https://mail.google.com/,https://www.googleapis.com/auth/drive,https://www.googleapis.com/auth/calendar,openid,email,profile}

[[ $(id -un) == atlas-v5 ]] || { echo "Run as atlas-v5: sudo -u atlas-v5 $0" >&2; exit 1; }
[[ -x ${GWS} ]] || { echo "gws executable not found: ${GWS}" >&2; exit 1; }
[[ -d ${CONFIG_DIR} ]] || { echo "Google Workspace config directory not found: ${CONFIG_DIR}" >&2; exit 1; }
[[ -f ${CONFIG_DIR}/client_secret.json ]] || { echo "Google OAuth client config not found: ${CONFIG_DIR}/client_secret.json" >&2; exit 1; }

export GOOGLE_WORKSPACE_CLI_CONFIG_DIR="${CONFIG_DIR}"
export GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND=file

echo "Authorizing Atlas Google Workspace with explicit Drive, Calendar and full Gmail scopes."
echo "OAuth callback is server-local; use an SSH local-forward when opening the consent URL on another device."
exec "${GWS}" auth login --scopes "${SCOPES}"
