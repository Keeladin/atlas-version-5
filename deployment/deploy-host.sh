#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run this script with sudo/root." >&2
  exit 1
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_ROOT=/opt/atlas-v5
APP_DIR=${APP_ROOT}/app
VENV_DIR=${APP_ROOT}/venv
UV_BIN=/home/jaco/.local/bin/uv
PYTHON_VERSION_FILE="${ROOT_DIR}/.python-version"
PYTHON_INSTALL_DIR=${APP_ROOT}/python

if ! id atlas-v5 >/dev/null 2>&1; then
  echo "atlas-v5 system identity is missing; run bootstrap-host.sh first." >&2
  exit 1
fi

if [[ ! -r /etc/atlas-v5/secrets/database-url ]]; then
  echo "Atlas V5 database secret is missing." >&2
  exit 1
fi
if [[ ! -r /etc/atlas-v5/secrets/openai-api-key ]]; then
  echo "Atlas V5 OpenAI API secret is missing." >&2
  exit 1
fi

if [[ ! -x ${UV_BIN} ]]; then
  echo "uv was not found at ${UV_BIN}." >&2
  exit 1
fi
if [[ ! -f ${PYTHON_VERSION_FILE} ]]; then
  echo "Atlas Python version pin is missing." >&2
  exit 1
fi
PYTHON_VERSION="$(tr -d '[:space:]' < "${PYTHON_VERSION_FILE}")"
if [[ -z ${PYTHON_VERSION} ]]; then
  echo "Atlas Python version pin is empty." >&2
  exit 1
fi
if [[ ! -f ${ROOT_DIR}/frontend/dist/index.html ]]; then
  echo "Frontend production build is missing. Run 'cd frontend && npm run build' first." >&2
  exit 1
fi

# Maintenance starts before changing any installed files or dependencies.
# On failure leave the runtime stopped; do not restart mixed/partial code.
if systemctl cat atlas-v5.service >/dev/null 2>&1; then
  systemctl stop atlas-v5.service
fi

install -d -o root -g atlas-v5 -m 0750 \
  "${APP_DIR}" \
  "${APP_DIR}/backend" \
  "${APP_DIR}/migrations" \
  "${APP_DIR}/frontend" \
  "${APP_DIR}/frontend/dist" \
  "${PYTHON_INSTALL_DIR}"

# Owner workspace stays outside Atlas runtime state. Atlas sees only this approved root.
install -d -o jaco -g atlas-v5 -m 2770 \
  /home/jaco/Workspace \
  /home/jaco/Workspace/Projects \
  /home/jaco/Workspace/Documents \
  /home/jaco/Workspace/Imports \
  /home/jaco/Workspace/Exports \
  /home/jaco/Workspace/Scratch
install -d -o atlas-v5 -g atlas-v5 -m 0770 /var/lib/atlas-v5/workspace
install -d -o atlas-v5 -g atlas-v5 -m 0750 /var/lib/atlas-v5/projects
install -d -o atlas-v5 -g atlas-v5 -m 0700 /var/lib/atlas-v5/auth /var/lib/atlas-v5/project-checkpoints /var/lib/atlas-v5/control

# Project ACLs are provisioned separately by reconcile-project-access.sh.
# Deployments must not recursively rewrite owner project permissions.
if ! command -v setfacl >/dev/null 2>&1; then
  echo "setfacl is required for Atlas maintenance access." >&2
  exit 1
fi

if ! grep -q '^ATLAS_WORKSPACE_ROOT=' /etc/atlas-v5/config/runtime.env; then
  echo 'ATLAS_WORKSPACE_ROOT=/var/lib/atlas-v5/workspace' >> /etc/atlas-v5/config/runtime.env
fi
if ! grep -q '^ATLAS_WORKSPACE_DISPLAY_ROOT=' /etc/atlas-v5/config/runtime.env; then
  echo 'ATLAS_WORKSPACE_DISPLAY_ROOT=/home/jaco/Workspace' >> /etc/atlas-v5/config/runtime.env
fi
if ! grep -q '^ATLAS_PROJECTS_ROOT=' /etc/atlas-v5/config/runtime.env; then
  echo 'ATLAS_PROJECTS_ROOT=/var/lib/atlas-v5/projects' >> /etc/atlas-v5/config/runtime.env
fi
if ! grep -q '^ATLAS_PROJECTS_DISPLAY_ROOT=' /etc/atlas-v5/config/runtime.env; then
  echo 'ATLAS_PROJECTS_DISPLAY_ROOT=/home/jaco/Projects' >> /etc/atlas-v5/config/runtime.env
fi
if ! grep -q '^ATLAS_PROJECT_CHECKPOINT_ROOT=' /etc/atlas-v5/config/runtime.env; then
  echo 'ATLAS_PROJECT_CHECKPOINT_ROOT=/var/lib/atlas-v5/project-checkpoints' >> /etc/atlas-v5/config/runtime.env
fi
if ! grep -q '^ATLAS_OWNER_TIMEZONE=' /etc/atlas-v5/config/runtime.env; then
  echo 'ATLAS_OWNER_TIMEZONE=Africa/Johannesburg' >> /etc/atlas-v5/config/runtime.env
fi
if ! grep -q '^ATLAS_SCHEDULER_ENABLED=' /etc/atlas-v5/config/runtime.env; then
  echo 'ATLAS_SCHEDULER_ENABLED=true' >> /etc/atlas-v5/config/runtime.env
fi
if ! grep -q '^ATLAS_SCHEDULER_POLL_SECONDS=' /etc/atlas-v5/config/runtime.env; then
  echo 'ATLAS_SCHEDULER_POLL_SECONDS=30' >> /etc/atlas-v5/config/runtime.env
fi
if ! grep -q '^ATLAS_ACTION_RECONCILE_SECONDS=' /etc/atlas-v5/config/runtime.env; then
  echo 'ATLAS_ACTION_RECONCILE_SECONDS=60' >> /etc/atlas-v5/config/runtime.env
fi
if ! grep -q '^ATLAS_ACTION_STALE_AFTER_SECONDS=' /etc/atlas-v5/config/runtime.env; then
  echo 'ATLAS_ACTION_STALE_AFTER_SECONDS=300' >> /etc/atlas-v5/config/runtime.env
fi
if ! grep -q '^ATLAS_AUTH_REQUIRED=' /etc/atlas-v5/config/runtime.env; then
  echo 'ATLAS_AUTH_REQUIRED=true' >> /etc/atlas-v5/config/runtime.env
fi
if ! grep -q '^ATLAS_AUTH_RP_ID=' /etc/atlas-v5/config/runtime.env; then
  echo 'ATLAS_AUTH_RP_ID=atlas-agentic.co.za' >> /etc/atlas-v5/config/runtime.env
fi
if grep -qx 'ATLAS_AUTH_RP_NAME=Atlas V5' /etc/atlas-v5/config/runtime.env; then
  sed -i 's/^ATLAS_AUTH_RP_NAME=Atlas V5$/ATLAS_AUTH_RP_NAME="Atlas V5"/' /etc/atlas-v5/config/runtime.env
elif ! grep -q '^ATLAS_AUTH_RP_NAME=' /etc/atlas-v5/config/runtime.env; then
  echo 'ATLAS_AUTH_RP_NAME="Atlas V5"' >> /etc/atlas-v5/config/runtime.env
fi
if ! grep -q '^ATLAS_AUTH_ORIGIN=' /etc/atlas-v5/config/runtime.env; then
  echo 'ATLAS_AUTH_ORIGIN=https://atlas-agentic.co.za' >> /etc/atlas-v5/config/runtime.env
fi
if ! grep -q '^ATLAS_AUTH_SESSION_HOURS=' /etc/atlas-v5/config/runtime.env; then
  echo 'ATLAS_AUTH_SESSION_HOURS=24' >> /etc/atlas-v5/config/runtime.env
fi
if ! grep -q '^ATLAS_AUTH_ENROLLMENT_CODE_FILE=' /etc/atlas-v5/config/runtime.env; then
  echo 'ATLAS_AUTH_ENROLLMENT_CODE_FILE=/var/lib/atlas-v5/auth/enrollment-code' >> /etc/atlas-v5/config/runtime.env
fi
if ! grep -q '^ATLAS_AUTH_ENROLLED_MARKER_FILE=' /etc/atlas-v5/config/runtime.env; then
  echo 'ATLAS_AUTH_ENROLLED_MARKER_FILE=/var/lib/atlas-v5/auth/enrolled' >> /etc/atlas-v5/config/runtime.env
fi

if [[ ! -f /var/lib/atlas-v5/auth/enrolled && ! -f /var/lib/atlas-v5/auth/enrollment-code ]]; then
  enrollment_code="$(openssl rand -hex 16)"
  printf '%s\n' "${enrollment_code}" > /var/lib/atlas-v5/auth/enrollment-code
  chown atlas-v5:atlas-v5 /var/lib/atlas-v5/auth/enrollment-code
  chmod 0600 /var/lib/atlas-v5/auth/enrollment-code
fi

UV_PYTHON_INSTALL_DIR="${PYTHON_INSTALL_DIR}" "${UV_BIN}" python install "${PYTHON_VERSION}" --no-bin
PYTHON_BIN="$(UV_PYTHON_INSTALL_DIR="${PYTHON_INSTALL_DIR}" "${UV_BIN}" python find "${PYTHON_VERSION}" --managed-python --no-project --resolve-links)"
chown -R root:atlas-v5 "${PYTHON_INSTALL_DIR}"
find "${PYTHON_INSTALL_DIR}" -type d -exec chmod 0750 {} +
find "${PYTHON_INSTALL_DIR}" -type f -exec chmod u=rw,g=r,o= {} +
find "${PYTHON_INSTALL_DIR}" -path '*/bin/*' -type f -exec chmod u=rwx,g=rx,o= {} +

rsync -a --delete \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  "${ROOT_DIR}/backend/" "${APP_DIR}/backend/"
rsync -a --delete "${ROOT_DIR}/migrations/" "${APP_DIR}/migrations/"
rsync -a --delete "${ROOT_DIR}/frontend/dist/" "${APP_DIR}/frontend/dist/"
install -o root -g atlas-v5 -m 0640 \
  "${ROOT_DIR}/pyproject.toml" "${APP_DIR}/pyproject.toml"
install -o root -g atlas-v5 -m 0640 \
  "${ROOT_DIR}/uv.lock" "${APP_DIR}/uv.lock"
install -o root -g atlas-v5 -m 0640 \
  "${ROOT_DIR}/alembic.ini" "${APP_DIR}/alembic.ini"

find "${APP_DIR}" -type d -exec chmod 0750 {} +
find "${APP_DIR}" -type f -exec chmod 0640 {} +
chown -R root:atlas-v5 "${APP_DIR}"
export UV_PROJECT_ENVIRONMENT="${VENV_DIR}"
"${UV_BIN}" sync \
  --project "${APP_DIR}" \
  --python "${PYTHON_BIN}" \
  --frozen \
  --no-dev

chown -R root:atlas-v5 "${VENV_DIR}"
find "${VENV_DIR}" -type d -exec chmod 0750 {} +
find "${VENV_DIR}" -type f -exec chmod u=rw,g=r,o= {} +
find "${VENV_DIR}/bin" -type f -exec chmod u=rwx,g=rx,o= {} +

bash "${ROOT_DIR}/deployment/grant-maintenance-access.sh"

# Schema migrations may introduce constraints the currently running code does
# not understand. Stop the old runtime before migration and fail closed: if
# Alembic fails, set -e exits here and Atlas remains stopped for inspection.

runuser -u atlas-v5 -- /bin/bash -c '
  set -a
  source /etc/atlas-v5/config/runtime.env
  set +a
  cd /opt/atlas-v5/app
  /opt/atlas-v5/venv/bin/alembic upgrade head
'

install -o root -g root -m 0644 \
  "${ROOT_DIR}/deployment/systemd/atlas-v5.service" \
  /etc/systemd/system/atlas-v5.service
systemctl daemon-reload
systemctl enable atlas-v5.service
systemctl start atlas-v5.service

health_url="http://127.0.0.1:8086/api/auth/status"
for attempt in {1..30}; do
  if curl --fail --silent --show-error --max-time 2 "${health_url}" >/dev/null; then
    break
  fi
  if [[ ${attempt} -eq 30 ]]; then
    echo "Atlas V5 did not become healthy after start." >&2
    systemctl --no-pager --full status atlas-v5.service || true
    journalctl -u atlas-v5.service -n 50 --no-pager || true
    exit 1
  fi
  sleep 1
done

systemctl --no-pager --full status atlas-v5.service || true

echo
echo "Atlas V5 deployment completed and health check passed."
echo "Runtime: http://127.0.0.1:8086"
if [[ ! -f /var/lib/atlas-v5/auth/enrolled && -f /var/lib/atlas-v5/auth/enrollment-code ]]; then
  echo "Owner passkey enrollment code: $(cat /var/lib/atlas-v5/auth/enrollment-code)"
  echo "Open https://atlas-agentic.co.za and use this code once to register your passkey."
fi
echo "Caddy/V4 were not changed."
