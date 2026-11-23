#!/bin/sh
# Start the Admin Portal (backend + Postgres, then frontend) next to Developer Edition.
#   Admin UI:          http://localhost:3006   (admin@zadmin.com / SEED_PASSWORD)
#   Registration API:  http://localhost:4102/v1
#
# ADMIN_PORTAL_ROOT: the folder holding InaiEra_admin_portal_backend and _frontend
# (default: adminportal_developeredition next to the developer_edition folder).
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=${ADMIN_PORTAL_ROOT:-$(cd "$HERE/../../.." && pwd)/adminportal_developeredition}
# Read by compose.override.yml for the support-schema stub's absolute path.
export DEVEDITION_ADMIN_PORTAL_DIR="$HERE"
docker compose -p inaiera_admin_portal_backend \
  -f "$ROOT/InaiEra_admin_portal_backend/docker-compose.yml" \
  -f "$HERE/compose.override.yml" up -d --build
docker compose -f "$ROOT/InaiEra_admin_portal_frontend/docker-compose.yml" up -d --build
