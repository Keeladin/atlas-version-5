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

if ! id atlas-v5 >/dev/null 2>&1; then
  echo "atlas-v5 system identity is missing; run bootstrap-host.sh first." >&2
  exit 1
fi

if [[ ! -r /etc/atlas-v5/secrets/database-url ]]; then
  echo "Atlas V5 database secret is missing." >&2
  exit 1
fi

if [[ ! -x ${UV_BIN} ]]; then
  echo "uv was not found at ${UV_BIN}." >&2
  exit 1
fi
if [[ ! -f ${ROOT_DIR}/frontend/dist/index.html ]]; then
  echo "Frontend production build is missing. Run 'cd frontend && npm run build' first." >&2
  exit 1
fi

install -d -o root -g atlas-v5 -m 0750 \
  "${APP_DIR}" \
  "${APP_DIR}/backend" \
  "${APP_DIR}/migrations" \
  "${APP_DIR}/frontend" \
  "${APP_DIR}/frontend/dist"

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
  --frozen \
  --no-dev

chown -R root:atlas-v5 "${VENV_DIR}"
find "${VENV_DIR}" -type d -exec chmod 0750 {} +
find "${VENV_DIR}" -type f -exec chmod u=rw,g=r,o= {} +
find "${VENV_DIR}/bin" -type f -exec chmod u=rwx,g=rx,o= {} +

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
systemctl restart atlas-v5.service

health_url="http://127.0.0.1:8086/api/health"
for attempt in {1..30}; do
  if curl --fail --silent --show-error --max-time 2 "${health_url}" >/dev/null; then
    break
  fi
  if [[ ${attempt} -eq 30 ]]; then
    echo "Atlas V5 did not become healthy after restart." >&2
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
echo "Caddy/V4 were not changed."
