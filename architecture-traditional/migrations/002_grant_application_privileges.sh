#!/usr/bin/env bash
set -Eeuo pipefail

: "${APP_DB_USER:?APP_DB_USER is required}"

psql \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --set=ON_ERROR_STOP=1 \
  --set=app_db_user="$APP_DB_USER" \
  --set=postgres_db="$POSTGRES_DB" <<'SQL'
REVOKE CONNECT ON DATABASE :"postgres_db" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"postgres_db" TO :"app_db_user";

REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO :"app_db_user";

REVOKE ALL ON TABLE
    clinical_records,
    authorization_rules,
    audit_events
FROM PUBLIC;

GRANT SELECT, INSERT
ON TABLE clinical_records
TO :"app_db_user";

GRANT SELECT, INSERT, UPDATE
ON TABLE authorization_rules
TO :"app_db_user";

GRANT SELECT, INSERT
ON TABLE audit_events
TO :"app_db_user";
SQL
