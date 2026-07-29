#!/usr/bin/env bash
set -Eeuo pipefail

: "${APP_DB_USER:?APP_DB_USER is required}"
: "${APP_DB_PASSWORD:?APP_DB_PASSWORD is required}"

if [ "$APP_DB_USER" = "$POSTGRES_USER" ]; then
  echo "APP_DB_USER must differ from POSTGRES_ADMIN_USER" >&2
  exit 1
fi

psql \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --set=ON_ERROR_STOP=1 \
  --set=app_db_user="$APP_DB_USER" \
  --set=app_db_password="$APP_DB_PASSWORD" <<'SQL'
SELECT format(
    'CREATE ROLE %I LOGIN PASSWORD %L '
    'NOSUPERUSER NOCREATEDB NOCREATEROLE '
    'NOINHERIT NOREPLICATION NOBYPASSRLS',
    :'app_db_user',
    :'app_db_password'
)
\gexec
SQL
