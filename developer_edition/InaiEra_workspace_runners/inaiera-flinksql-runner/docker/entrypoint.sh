#!/usr/bin/env bash
# ==============================================================================
# Single-image entrypoint for the InAiEra FlinkSQL Runner.
#
# Boots the whole stack inside ONE container, in order:
#   1. Apache Flink standalone session cluster (JobManager + TaskManager)
#   2. Flink SQL Gateway (REST :8083)
#   3. Database migrations (Alembic)
#   4. The FastAPI Runner API (:8080)  ← foreground process
#
# Flink is the compute engine and lives *inside* this image, so the Runner API
# talks to it over localhost. Postgres / Kafka / Redis are external systems
# (like a dbt warehouse) and are reached via the *_URL / *_SERVERS env vars.
# ==============================================================================
set -euo pipefail

FLINK_HOME="${FLINK_HOME:-/opt/flink}"
log() { echo "[entrypoint] $*"; }

# ── Point the Runner API at the in-container Flink by default ─────────────────
export FLINK_GATEWAY_URL="${FLINK_GATEWAY_URL:-http://localhost:8083}"
export FLINK_REST_URL="${FLINK_REST_URL:-http://localhost:8081}"
# Alembic (migrations/env.py) reads DATABASE_URL from the environment directly,
# so mirror the app's default when the operator hasn't supplied one.
export DATABASE_URL="${DATABASE_URL:-postgresql+asyncpg://flinksql:flinksql@localhost:5432/flinksql}"

RUNNER_HOST="${RUNNER_HOST:-0.0.0.0}"
RUNNER_PORT="${RUNNER_PORT:-8080}"

mkdir -p /tmp/flink-checkpoints /tmp/flink-savepoints

# ── 1 + 2. Start Flink daemons ────────────────────────────────────────────────
log "starting Flink standalone session cluster..."
"$FLINK_HOME/bin/start-cluster.sh"

log "starting Flink SQL Gateway on :8083..."
"$FLINK_HOME/bin/sql-gateway.sh" start

# Stream Flink logs to the container's stdout so `docker logs` shows everything.
tail -n +1 -F "$FLINK_HOME"/log/*.log 2>/dev/null &
TAIL_PID=$!

# ── Graceful shutdown ──────────────────────────────────────────────────────────
API_PID=""
shutdown() {
  log "shutting down..."
  [ -n "$API_PID" ] && kill "$API_PID" 2>/dev/null || true
  "$FLINK_HOME/bin/sql-gateway.sh" stop           >/dev/null 2>&1 || true
  "$FLINK_HOME/bin/stop-cluster.sh"               >/dev/null 2>&1 || true
  kill "$TAIL_PID" 2>/dev/null || true
  wait 2>/dev/null || true
}
trap shutdown TERM INT

# ── Wait for Flink to become reachable ──────────────────────────────────────────
wait_for() { # url label max_tries
  local url="$1" label="$2" tries="${3:-60}" i
  for i in $(seq 1 "$tries"); do
    if curl -fsS "$url" >/dev/null 2>&1; then log "$label is up"; return 0; fi
    sleep 2
  done
  log "WARNING: $label ($url) did not become ready in time — continuing anyway"
  return 0   # advisory only: never abort the boot (set -e is on)
}
wait_for "http://localhost:8081/overview" "Flink JobManager" 60
wait_for "http://localhost:8083/info"     "Flink SQL Gateway" 120

# ── 3. Database migrations (retry; non-fatal so the API still serves /healthz) ──
log "applying database migrations..."
migrated=0
for i in $(seq 1 30); do
  if alembic upgrade head; then log "migrations applied"; migrated=1; break; fi
  log "database not ready yet (attempt $i/30) — retrying in 2s"
  sleep 2
done
[ "$migrated" -eq 1 ] || log "WARNING: migrations did not complete — API will start but /readyz will report the DB as down"

# ── 4. Runner API (foreground; container lifetime == API lifetime) ──────────────
log "starting FlinkSQL Runner API on ${RUNNER_HOST}:${RUNNER_PORT}"
uvicorn app.main:app --host "$RUNNER_HOST" --port "$RUNNER_PORT" &
API_PID=$!
wait "$API_PID"
