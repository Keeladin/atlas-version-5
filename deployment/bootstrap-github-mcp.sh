#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run this script with sudo/root." >&2
  exit 1
fi

GITHUB_USER=${GITHUB_USER:-jaco}
GITHUB_REPO=${GITHUB_MCP_REPO:-github/github-mcp-server}
GITHUB_MCP_VERSION=${GITHUB_MCP_VERSION:-v1.12.0}
RUNTIME_ENV=/etc/atlas-v5/config/runtime.env
BINARY_TARGET=/opt/atlas-v5/bin/github-mcp-server
TOKEN_TARGET=/etc/atlas-v5/secrets/github-token

[[ -f ${RUNTIME_ENV} ]] || { echo "Atlas runtime env not found: ${RUNTIME_ENV}" >&2; exit 1; }
command -v gh >/dev/null 2>&1 || { echo "GitHub CLI (gh) is required." >&2; exit 1; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
chown "${GITHUB_USER}" "${TMP}"
chmod 0750 "${TMP}"

sudo -u "${GITHUB_USER}" -H gh auth token > "${TMP}/github-token"
[[ -s ${TMP}/github-token ]] || { echo "GitHub CLI has no active token for ${GITHUB_USER}." >&2; exit 1; }

sudo -u "${GITHUB_USER}" -H gh release download "${GITHUB_MCP_VERSION}" \
  --repo "${GITHUB_REPO}" \
  --pattern 'github-mcp-server_Linux_x86_64.tar.gz' \
  --pattern 'github-mcp-server_*_checksums.txt' \
  --dir "${TMP}"

ARCHIVE="${TMP}/github-mcp-server_Linux_x86_64.tar.gz"
CHECKSUMS=$(find "${TMP}" -maxdepth 1 -name '*checksums.txt' -print -quit)
[[ -f ${ARCHIVE} && -n ${CHECKSUMS} ]] || { echo "GitHub MCP release download incomplete." >&2; exit 1; }
(
  cd "${TMP}"
  grep 'github-mcp-server_Linux_x86_64.tar.gz$' "${CHECKSUMS}" | sha256sum -c -
)
tar -xzf "${ARCHIVE}" -C "${TMP}"
[[ -x ${TMP}/github-mcp-server ]] || chmod +x "${TMP}/github-mcp-server"

install -d -o root -g atlas-v5 -m 0750 /opt/atlas-v5/bin /etc/atlas-v5/secrets
install -o root -g atlas-v5 -m 0750 "${TMP}/github-mcp-server" "${BINARY_TARGET}"
install -o root -g atlas-v5 -m 0640 "${TMP}/github-token" "${TOKEN_TARGET}"

set_env() {
  local key=$1 value=$2
  if grep -q "^${key}=" "${RUNTIME_ENV}"; then
    sed -i "s|^${key}=.*|${key}=${value}|" "${RUNTIME_ENV}"
  else
    printf '%s=%s\n' "${key}" "${value}" >> "${RUNTIME_ENV}"
  fi
}

set_env ATLAS_GITHUB_MCP_COMMAND "${BINARY_TARGET}"
set_env ATLAS_GITHUB_TOKEN_FILE "${TOKEN_TARGET}"
set_env ATLAS_GITHUB_MCP_TOOLSETS "repos,git,pull_requests,issues"
chown root:atlas-v5 "${RUNTIME_ENV}"
chmod 0640 "${RUNTIME_ENV}"

echo "GitHub MCP is provisioned for Atlas V5 in read-only mode."
echo "Mutation tools remain disabled at the MCP server boundary."
