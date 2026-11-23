-- Runs automatically on first container startup (docker-entrypoint-initdb.d
-- only runs against an EMPTY data volume — if you've already got a
-- populated ml-studio-db-data volume from before this file existed,
-- `docker compose down -v` first, or run this by hand against the running
-- container).
--
-- POSTGRES_DB=mlstudio (set in docker-compose.yml) already creates the
-- mlstudio database as part of the image's own entrypoint before this
-- script runs — this file adds the other two this platform needs, so one
-- `docker compose up -d` is enough for all three services, not just this one.

CREATE USER ucs WITH PASSWORD 'ucs' SUPERUSER;
CREATE DATABASE ucs OWNER ucs;
CREATE DATABASE model_import_service;
