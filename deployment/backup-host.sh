#!/usr/bin/env bash
set -euo pipefail
if [[ ${EUID} -ne 0 || $# -ne 1 ]]; then
  echo 'Usage: sudo backup-host.sh /var/lib/atlas-v5/backups/NEW-SNAPSHOT' >&2
  exit 1
fi
for unit in atlas-v5.service atlas-v5-memory.service atlas-v5-memory.timer; do
  if systemctl is-active --quiet "${unit}"; then
    echo "Stop ${unit} before taking a consistent offline backup." >&2
    exit 1
  fi
done
runuser -u atlas-v5 -- /bin/bash -c '
  set -a
  source /etc/atlas-v5/config/runtime.env
  set +a
  exec /opt/atlas-v5/venv/bin/python -m atlas.maintenance.backup backup "$1" --offline
' atlas-backup "$1"
