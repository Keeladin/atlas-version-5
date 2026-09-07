#!/usr/bin/env bash
set -euo pipefail

GWS=${GWS_COMMAND:-/home/jaco/Projects/atlas-agent-state/production/bin/gws}
CONFIG_DIR=${GWS_CONFIG_DIR:-/home/jaco/Projects/atlas-agent-state/production/google-workspace/config}

[[ -x ${GWS} ]] || { echo "gws executable not found: ${GWS}" >&2; exit 1; }
[[ -d ${CONFIG_DIR} ]] || { echo "Google Workspace config directory not found: ${CONFIG_DIR}" >&2; exit 1; }

export GOOGLE_WORKSPACE_CLI_CONFIG_DIR="${CONFIG_DIR}"
export GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND=file

exec "${GWS}" auth login --services drive,gmail,calendar
