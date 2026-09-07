#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run this script with sudo/root." >&2
  exit 1
fi

PROJECTS_ROOT=/home/jaco/Projects
TARGET="${1:-${PROJECTS_ROOT}}"

if ! id atlas-v5 >/dev/null 2>&1; then
  echo "atlas-v5 system identity is missing." >&2
  exit 1
fi
if ! command -v setfacl >/dev/null 2>&1; then
  echo "setfacl is required for Atlas project access." >&2
  exit 1
fi
if [[ ! -d ${TARGET} ]]; then
  echo "Project access target is not a directory: ${TARGET}" >&2
  exit 1
fi

PROJECTS_ROOT_REAL="$(realpath -e "${PROJECTS_ROOT}")"
TARGET_REAL="$(realpath -e "${TARGET}")"
case "${TARGET_REAL}" in
  "${PROJECTS_ROOT_REAL}"|"${PROJECTS_ROOT_REAL}"/*) ;;
  *)
    echo "Refusing ACL changes outside ${PROJECTS_ROOT}." >&2
    exit 1
    ;;
esac

find "${TARGET_REAL}" \
  \( -name .git -o -name node_modules -o -name .venv -o -name __pycache__ \) -prune -o \
  -type d -exec setfacl -n -m \
    u:atlas-v5:rwx,u:jaco:rwx,m::rwx,d:u:atlas-v5:rwx,d:u:jaco:rwx,d:m::rwx {} +
find "${TARGET_REAL}" \
  \( -name .git -o -name node_modules -o -name .venv -o -name __pycache__ \) -prune -o \
  -type f -exec setfacl -n -m u:atlas-v5:rw-,u:jaco:rw-,m::rw- {} +

printf 'Atlas project access reconciled for: %s\n' "${TARGET_REAL}"
