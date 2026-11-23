#!/usr/bin/env bash
# FE-04: run the live (no-mock) UI end-to-end suite against the real Docker Compose stack: FastAPI + PostgreSQL + Kafka + Kafka Connect.
#
#   scripts/live-e2e.sh            start the stack if it is not up, then run the suite
#   scripts/live-e2e.sh --no-up    only run (the stack is already up)
#   any other arguments are passed to Playwright, e.g.  scripts/live-e2e.sh -g "restart"
#
# INFRA_DIR  where docker-compose.yml lives (default: ../InaiEra_DataConnect_Backend/infra, the sibling checkout)
set -euo pipefail
cd "$(dirname "$0")/.."
INFRA_DIR="${INFRA_DIR:-../InaiEra_DataConnect_Backend/infra}"

UP=1
if [ "${1:-}" = "--no-up" ]; then UP=0; shift; fi

if [ "$UP" = "1" ]; then
  if [ ! -f "$INFRA_DIR/docker-compose.yml" ]; then
    echo "No docker-compose.yml in INFRA_DIR=$INFRA_DIR — check out InaiEra_DataConnect_Backend next to this repo or set INFRA_DIR." >&2
    exit 2
  fi
  echo "Starting the stack (docker compose up -d) in $INFRA_DIR ..."
  (cd "$INFRA_DIR" && docker compose up -d)
  echo -n "Waiting for the backend and Kafka Connect "
  for _ in $(seq 1 90); do
    if curl -fsS http://localhost:8001/api/v1/dataconnect/health/ready >/dev/null 2>&1 && curl -fsS http://localhost:8083/connectors >/dev/null 2>&1; then echo " ready"; break; fi
    echo -n "."; sleep 2
  done
fi

DATACONNECT_LIVE_E2E=1 exec npx playwright test --project=live "$@"
