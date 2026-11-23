#!/bin/bash
# Developer Edition: ONE PostgreSQL server, one database + owner role per product
# (feature sheet rule 6). Runs once, on an empty data volume only.
# Every product role uses DE_DB_PASSWORD from .env; nothing listens outside Docker
# except the optional 127.0.0.1 port in docker-compose.yml.
set -euo pipefail

create_db() {  # create_db <role> <database> [extra role options]
  local role="$1" db="$2" opts="${3:-}"
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres <<-SQL
    DO \$\$ BEGIN
      IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '${role}') THEN
        CREATE ROLE "${role}" LOGIN PASSWORD '${DE_DB_PASSWORD}' ${opts};
      END IF;
    END \$\$;
SQL
  if ! psql --username "$POSTGRES_USER" -tAc "SELECT 1 FROM pg_database WHERE datname='${db}'" | grep -q 1; then
    psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" -c "CREATE DATABASE \"${db}\" OWNER \"${role}\""
  fi
}

create_db dataconnect  dataconnect
create_db inaiera      inaiera_agents  CREATEDB   # Temporal auto-setup creates temporal + temporal_visibility as this role
create_db mlstudio     mlstudio
create_db workspace    workspace
create_db dataquality  dataquality
create_db dbtrunner    dbtrunner
create_db flinksql     flinksql
create_db dagster      dagster
create_db devuser      userdata                    # the user's own pgvector database

# pgvector: Agents knowledge bases and the user's own database.
for db in inaiera_agents userdata; do
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$db" -c "CREATE EXTENSION IF NOT EXISTS vector"
done
