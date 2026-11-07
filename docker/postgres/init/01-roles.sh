#!/bin/sh
# Runs once on first initialisation of the data directory.
# Passwords arrive via environment and are passed to psql as variables.
set -eu

psql -v ON_ERROR_STOP=1 -U postgres -d postgres \
  -v mig_pw="$QUARTET_MIGRATOR_PASSWORD" \
  -v app_pw="$QUARTET_APP_PASSWORD" <<'SQL'
CREATE ROLE quartet_migrator LOGIN PASSWORD :'mig_pw';
CREATE ROLE quartet_app LOGIN PASSWORD :'app_pw';
ALTER DATABASE quartet OWNER TO quartet_migrator;
SQL
