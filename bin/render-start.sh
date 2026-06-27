#!/usr/bin/env bash
set -euo pipefail

PORT="${PORT:-10000}"
SQLITE_PATH="${SQLITE_PATH:-/app/data/db.sqlite3}"
MEDIA_ROOT="${MEDIA_ROOT:-/app/media}"
FILE_UPLOAD_TEMP_DIR="${FILE_UPLOAD_TEMP_DIR:-/tmp/mcqanchor-uploads}"
export SQLITE_PATH
export MEDIA_ROOT
export FILE_UPLOAD_TEMP_DIR

mkdir -p "$(dirname "$SQLITE_PATH")"
mkdir -p "$MEDIA_ROOT"
mkdir -p "$FILE_UPLOAD_TEMP_DIR"

python manage.py migrate --noinput
python manage.py ensure_admin_user
python manage.py collectstatic --noinput

if [[ "${QUESTION_BANK_BUILDER_LOOP_ENABLED:-false}" =~ ^(1|true|TRUE|yes|YES|on|ON)$ ]]; then
  python manage.py run_question_bank_builder &
fi

exec gunicorn config.wsgi:application \
  --bind "0.0.0.0:${PORT}" \
  --workers "${WEB_CONCURRENCY:-2}" \
  --timeout "${GUNICORN_TIMEOUT:-120}"
