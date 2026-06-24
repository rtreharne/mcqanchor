#!/usr/bin/env bash
set -euo pipefail

PORT="${PORT:-10000}"
SQLITE_PATH="${SQLITE_PATH:-/app/data/db.sqlite3}"
MEDIA_ROOT="${MEDIA_ROOT:-/app/media}"
export SQLITE_PATH
export MEDIA_ROOT

mkdir -p "$(dirname "$SQLITE_PATH")"
mkdir -p "$MEDIA_ROOT"

python manage.py migrate --noinput
python manage.py ensure_admin_user
python manage.py collectstatic --noinput

exec gunicorn config.wsgi:application \
  --bind "0.0.0.0:${PORT}" \
  --workers "${WEB_CONCURRENCY:-2}" \
  --timeout "${GUNICORN_TIMEOUT:-120}"
