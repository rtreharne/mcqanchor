#!/usr/bin/env bash
set -euo pipefail

PORT="${PORT:-10000}"
SQLITE_PATH="${SQLITE_PATH:-/app/data/db.sqlite3}"
export SQLITE_PATH

mkdir -p "$(dirname "$SQLITE_PATH")"

python manage.py migrate --noinput
python manage.py ensure_admin_user
python manage.py collectstatic --noinput

exec gunicorn config.wsgi:application \
  --bind "0.0.0.0:${PORT}" \
  --workers "${WEB_CONCURRENCY:-2}" \
  --timeout "${GUNICORN_TIMEOUT:-120}"
