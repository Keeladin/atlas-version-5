#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run this script with sudo/root." >&2
  exit 1
fi

APP_USER="atlas-v5"
APP_GROUP="atlas-v5"
DB_ROLE="atlas_v5"
DB_NAME="atlas_v5"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

apt-get update
apt-get install -y postgresql-18 postgresql-18-pgvector openssl rsync acl
systemctl enable --now postgresql

if ! id "${APP_USER}" >/dev/null 2>&1; then
  useradd --system --user-group --home-dir /nonexistent --shell /usr/sbin/nologin "${APP_USER}"
fi

if [[ -d /home/jaco/Projects ]]; then
  bash "${ROOT_DIR}/deployment/reconcile-project-access.sh" /home/jaco/Projects
fi

install -d -o "${APP_USER}" -g "${APP_GROUP}" -m 0700 \
  /var/lib/atlas-v5/artifacts /var/lib/atlas-v5/runtime /var/lib/atlas-v5/backups /var/lib/atlas-v5/control
install -d -o "${APP_USER}" -g "${APP_GROUP}" -m 0750 /var/lib/atlas-v5/observer
install -d -o root -g "${APP_GROUP}" -m 0750 \
  /etc/atlas-v5/config /etc/atlas-v5/secrets /opt/atlas-v5

DB_PASSWORD="$(openssl rand -hex 32)"

if sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='${DB_ROLE}'" | grep -q 1; then
  sudo -u postgres psql --set=ON_ERROR_STOP=1 --set=db_password="${DB_PASSWORD}" <<'SQL'
ALTER ROLE atlas_v5 WITH LOGIN PASSWORD :'db_password';
SQL
else
  sudo -u postgres psql --set=ON_ERROR_STOP=1 --set=db_password="${DB_PASSWORD}" <<'SQL'
CREATE ROLE atlas_v5 WITH LOGIN PASSWORD :'db_password';
SQL
fi

if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" | grep -q 1; then
  sudo -u postgres createdb --owner="${DB_ROLE}" "${DB_NAME}"
fi

sudo -u postgres psql --set=ON_ERROR_STOP=1 --dbname="${DB_NAME}" \
  --command="CREATE EXTENSION IF NOT EXISTS vector;"

DATABASE_URL="postgresql+psycopg://${DB_ROLE}:${DB_PASSWORD}@127.0.0.1/${DB_NAME}"
printf '%s\n' "${DATABASE_URL}" > /etc/atlas-v5/secrets/database-url
chown root:"${APP_GROUP}" /etc/atlas-v5/secrets/database-url
chmod 0640 /etc/atlas-v5/secrets/database-url

install -o root -g "${APP_GROUP}" -m 0640 \
  "${ROOT_DIR}/deployment/runtime.env.example" /etc/atlas-v5/config/runtime.env

bash "${ROOT_DIR}/deployment/grant-maintenance-access.sh"

cat <<EOF
Atlas V5 host foundation is ready.

Created/verified:
  system user: ${APP_USER}
  PostgreSQL database: ${DB_NAME}
  PostgreSQL role: ${DB_ROLE}
  state root: /var/lib/atlas-v5
  secret/config root: /etc/atlas-v5
  deployment root: /opt/atlas-v5

The generated database credential is stored only in:
  /etc/atlas-v5/secrets/database-url
EOF
