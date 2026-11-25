#!/usr/bin/env bash
# The platform gate (X-007): what no single repository's CI can check, in the order it has to run.
#
#   scripts/platform_gate.sh            static gate: every repo's own suite + the cross-repo contract tests + chart lint + frontend drift
#   scripts/platform_gate.sh --e2e      the static gate, then the live end-to-end scenarios against a running stack (scripts/e2e)
#   scripts/platform_gate.sh --e2e-only just the live scenarios
#
# The sibling repositories are expected next to this one (override with RUNNERS_REPO / DQ_REPO / FRONTEND_REPO / DAGSTER_REPO); one
# that is missing is skipped LOUDLY. Needs: python with each repo's dev dependencies, helm, and for the database-backed suites
# TEST_DATABASE_URL (the DQ / runner suites) and INTEGRATION_DATABASE_URL (Workspace's real-DB tests) pointing at throwaway Postgres
# databases that may be wiped. The Flink cluster chart needs `helm dependency build` (network) and is checked by the CI job, not here.
set -uo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
ROOT="$(dirname "$HERE")"
PY="${PYTHON:-python}"
RUNNERS="${RUNNERS_REPO:-$ROOT/Pralai_workspace_runners}"
DQ="${DQ_REPO:-$ROOT/Pralai_dataquality_backend}"
FRONTEND="${FRONTEND_REPO:-$ROOT/Pralai_workspace_frontend}"
DAGSTER="${DAGSTER_REPO:-$ROOT/Pralai-dbt-runner-dagster}"
MODE="${1:-static}"
FAILED=()
SKIPPED=()

step() {   # step <name> <dir> <command...>
  local name="$1" dir="$2"; shift 2
  if [ ! -d "$dir" ]; then SKIPPED+=("$name (not checked out: $dir)"); echo "SKIP  $name"; return; fi
  echo "=== $name"
  if (cd "$dir" && "$@"); then echo "PASS  $name"; else FAILED+=("$name"); echo "FAIL  $name"; fi
}

if [ "$MODE" != "--e2e-only" ]; then
  # 1. every repo's own suite
  step "workspace backend suite"       "$HERE"                                  "$PY" -m pytest -q -p no:cacheprovider
  # each database-backed suite gets its own throwaway database: DQ_TEST_DATABASE_URL / DBT_TEST_DATABASE_URL / FLINK_TEST_DATABASE_URL
  step "dq suite"                      "$DQ"                                    env TEST_DATABASE_URL="${DQ_TEST_DATABASE_URL:-${TEST_DATABASE_URL:-}}" "$PY" -m pytest -q -p no:cacheprovider
  step "dbt runner suite"              "$RUNNERS/pralai-dbt-runner"            env TEST_DATABASE_URL="${DBT_TEST_DATABASE_URL:-${TEST_DATABASE_URL:-}}" "$PY" -m pytest -q -p no:cacheprovider
  step "flink runner suite"            "$RUNNERS/pralai-flinksql-runner"       env TEST_DATABASE_URL="${FLINK_TEST_DATABASE_URL:-${TEST_DATABASE_URL:-}}" "$PY" -m pytest -q -p no:cacheprovider
  step "dagster suite"                 "$DAGSTER"                               "$PY" -m pytest -q -p no:cacheprovider
  # 2. the cross-repo contracts, on their own so a failure here is unmistakable (they also run inside the workspace suite)
  step "cross-repo: charts vs settings, secrets, URLs"  "$HERE"  "$PY" -m pytest -q -p no:cacheprovider tests/test_platform_contract.py
  step "cross-repo: RBAC contract (roles, DQ table, runner tables, frontend copy)" "$HERE" "$PY" -m pytest -q -p no:cacheprovider tests/test_rbac_contract.py
  step "cross-repo: delegation tokens and DQ scopes"    "$HERE"  "$PY" -m pytest -q -p no:cacheprovider tests/test_delegated_tokens.py tests/test_dq_delegation.py
  step "committed contracts are current" "$HERE" bash -c "$PY scripts/export_rbac_contract.py --check contracts/rbac.contract.v1.json && $PY scripts/export_openapi.py --check openapi/workspace.json"
  # 3. every production chart lints and renders
  for chart in "$HERE/docker/helm/workspace" "$RUNNERS/pralai-dbt-runner/docker/helm/dbtrunner" "$RUNNERS/pralai-flinksql-runner/docker/helm/flinksql-runner" "$DQ/docker/helm/dq-service"; do
    extra=()
    # DBT-010: the dbt runner chart refuses to render until Job pods' egress destinations are named
    [ -f "$chart/ci/egress-values.yaml" ] && extra=(-f "$chart/ci/egress-values.yaml")
    step "helm lint $(basename "$chart")" "$chart/.." helm lint "$chart" --set tenant.orgId=acme --set orgId=acme "${extra[@]}"
  done
  # 4. the frontend: generated RBAC module and API types match their contracts
  if [ -d "$FRONTEND/node_modules/.bin" ]; then
    step "frontend: contracts and tests" "$FRONTEND" bash -c "npm run -s codegen:rbac:check && npm run -s typecheck && npx vitest run"
  else
    SKIPPED+=("frontend (dependencies not installed in $FRONTEND; run npm ci)"); echo "SKIP  frontend"
  fi
fi

if [ "$MODE" = "--e2e" ] || [ "$MODE" = "--e2e-only" ]; then
  step "live end-to-end scenarios" "$HERE" "$PY" scripts/e2e/platform_e2e.py
fi

echo
[ ${#SKIPPED[@]} -gt 0 ] && printf 'SKIPPED: %s\n' "${SKIPPED[@]}"
if [ ${#FAILED[@]} -gt 0 ]; then printf 'FAILED: %s\n' "${FAILED[@]}"; exit 1; fi
echo "platform gate passed"
