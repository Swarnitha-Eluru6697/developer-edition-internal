#!/usr/bin/env bash
# Helm chart render/lint tests for docker/helm/dbtrunner.
#
#   scripts/helm-render-test.sh            # uses docker images (no local helm needed)
#
# Checks: lint + render with defaults and with every integration enabled;
# hardening invariants in the rendered output; that invalid / incomplete
# values FAIL to render (`required` / `fail`); and that the rendered
# ConfigMap is accepted by the application's Settings (the pod can start).
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
CHART="$REPO/docker/helm/dbtrunner"
HELM_IMAGE="${HELM_IMAGE:-alpine/helm:3.14.4}"
APP_IMAGE="${APP_IMAGE:-dbtrunner-dbtrunner}"
OUT="$(mktemp -d)"
trap 'rm -rf "$OUT"' EXIT
fails=0

helm() { docker run --rm -v "$CHART":/c -v "$OUT":/out "$HELM_IMAGE" "$@"; }
pass() { echo "PASS: $*"; }
fail() { echo "FAIL: $*"; fails=$((fails + 1)); }

expect_render_ok() {  # name, extra helm args...
  local name="$1"; shift
  if helm template t /c "$@" > "$OUT/$name.yaml" 2> "$OUT/$name.err"; then pass "render $name"
  else fail "render $name: $(cat "$OUT/$name.err")"; fi
}

expect_render_fail() {  # name, expected-message, extra helm args...
  local name="$1" msg="$2"; shift 2
  if helm template t /c "$@" > /dev/null 2> "$OUT/$name.err"; then
    fail "$name: render unexpectedly succeeded"
  elif grep -q "$msg" "$OUT/$name.err"; then pass "$name fails with: $msg"
  else fail "$name failed for the wrong reason: $(cat "$OUT/$name.err")"; fi
}

has()   { grep -q -- "$2" "$OUT/$1.yaml" && pass "$1 contains $2" || fail "$1 missing $2"; }
lacks() { grep -q -- "$2" "$OUT/$1.yaml" && fail "$1 must not contain $2" || pass "$1 lacks $2"; }

# --- lint
helm lint /c -f /c/ci/egress-values.yaml > "$OUT/lint1" 2>&1 && pass "lint defaults" || fail "lint defaults: $(cat "$OUT/lint1")"
grep -q WARNING "$OUT/lint1" && fail "lint defaults has warnings: $(grep WARNING "$OUT/lint1")" || true
helm lint /c -f /c/ci/integrations-values.yaml > "$OUT/lint2" 2>&1 && pass "lint integrations" \
  || fail "lint integrations: $(cat "$OUT/lint2")"

# --- render
expect_render_ok defaults -f /c/ci/egress-values.yaml
expect_render_ok integrations -f /c/ci/integrations-values.yaml

has defaults 'ENVIRONMENT: "prod"'
lacks defaults '"production"'
lacks defaults 'DATABASE_URL'
lacks defaults '$(DATABASE_PASSWORD)'
lacks defaults 'sslmode'
has defaults 'DATABASE_SSL: "require"'
has defaults 'kind: NetworkPolicy'
has defaults 'name: dbt-runner-replace-me-job$'
has defaults 'kind: PodDisruptionBudget'
has defaults 'topologySpreadConstraints:'
has defaults 'type: RuntimeDefault'
has defaults 'automountServiceAccountToken: false'
has defaults 'RUNNER_JOB_SERVICE_ACCOUNT: "dbt-runner-replace-me-job"'
has defaults 'RUNNER_IMAGE: "registry.inaiera.com/products/dbt-runner:0.1.0"'
has defaults 'name: TRINO_JWT'
has defaults 'verbs: \["create"\]'
lacks defaults 'resourceNames:'
lacks defaults 'OPENLINEAGE_URL'
lacks defaults 'UCS_INTERNAL_TOKEN'
lacks defaults 'SERVICE_JWT_SECRET'
has integrations 'OPENLINEAGE_URL: "http://catalog.dataplane.svc:8000/api/v1/lineage"'
has integrations 'OPENLINEAGE_NAMESPACE: "inaiera.dbt"'
has integrations 'DQ_SERVICE_URL: "http://dataquality'
has integrations 'name: SERVICE_JWT_SECRET'
has integrations 'name: UCS_INTERNAL_TOKEN'
has integrations 'name: DATASETS_INTERNAL_TOKEN'
has integrations 'name: AWS_ACCESS_KEY_ID'
has integrations 'resourceNames:'
has integrations 'RUNNER_IMAGE: "registry.inaiera.com/products/dbt-runner-job:0.1.0"'

# every container (API + migrate) carries the hardened securityContext
n_containers=$(grep -c '^        - name: \(dbtrunner\|migrate\)$' "$OUT/defaults.yaml" || true)
n_seccomp=$(grep -c 'type: RuntimeDefault' "$OUT/defaults.yaml" || true)
[ "$n_seccomp" -ge $((n_containers * 2)) ] && pass "seccomp on all pods+containers ($n_seccomp)" \
  || fail "seccomp count $n_seccomp < $((n_containers * 2))"

# --- required / fail cases
# DBT-010: Job pods (customer dbt code) have no default route anywhere; destinations must be named
expect_render_fail egress-unconfigured "networkPolicy.job.git.to"
expect_render_fail egress-no-warehouse "networkPolicy.job.warehouse.to is required" \
  -f /c/ci/egress-values.yaml --set networkPolicy.job.warehouse.to=null
expect_render_fail public-internet-in-prod "allowPublicInternet is only allowed with environment=dev" \
  -f /c/ci/egress-values.yaml --set networkPolicy.job.allowPublicInternet=true
expect_render_fail openlineage-no-url "integrations.openlineage.url is required" \
  --set integrations.openlineage.enabled=true
expect_render_fail datasets-no-url "integrations.datasets.url is required" \
  --set integrations.datasets.enabled=true
expect_render_fail dq-no-url "integrations.dataQuality.serviceUrl is required" \
  --set integrations.dataQuality.enabled=true
expect_render_fail bad-environment "environment must be one of dev|staging|prod" \
  --set environment=production
expect_render_fail subprocess-in-prod "only allowed with environment=dev" \
  --set runner.mode=subprocess
expect_render_fail k8s-without-s3 "artifactStore.backend must be s3" \
  --set artifactStore.backend=local
expect_render_fail k8s-without-bucket "artifactStore.s3.bucket is required" \
  --set artifactStore.s3.bucket=

# --- the rendered ConfigMap is accepted by the app's Settings
for name in defaults integrations; do
  if docker run -i --rm -v "$OUT":/out:ro -v "$REPO/app":/app/app:ro -w /app --entrypoint python "$APP_IMAGE" - "$name" > "$OUT/$name.settings" 2>&1 <<'PY'
import sys, yaml, os
docs = [d for d in yaml.safe_load_all(open(f"/out/{sys.argv[1]}.yaml")) if d]
cm = next(d for d in docs if d["kind"] == "ConfigMap")
os.environ.update({k: str(v) for k, v in cm["data"].items()})
os.environ["DATABASE_PASSWORD"] = "p@ss:word/with#chars"
from app.core.config import Settings
s = Settings()
from sqlalchemy.engine import make_url
u = make_url(s.effective_database_url)
assert s.environment in ("dev", "staging", "prod"), s.environment
assert u.password == "p@ss:word/with#chars" and "sslmode" not in s.effective_database_url
assert s.database_connect_args() == {"ssl": "require"}
assert s.runner_job_pod_labels["app.kubernetes.io/instance"].startswith("dbt-runner-")
assert isinstance(s.cors_origins, list)
print("settings-ok", s.environment, s.runner_mode, s.artifact_store_backend)
PY
  then pass "Settings accepts rendered ConfigMap ($name): $(tail -1 "$OUT/$name.settings")"
  else fail "Settings rejects rendered ConfigMap ($name): $(cat "$OUT/$name.settings")"; fi
done

echo
if [ "$fails" -eq 0 ]; then echo "ALL HELM CHECKS PASSED"; else echo "$fails HELM CHECK(S) FAILED"; exit 1; fi
