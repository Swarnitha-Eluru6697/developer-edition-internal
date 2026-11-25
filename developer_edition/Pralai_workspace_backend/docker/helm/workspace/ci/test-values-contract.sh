#!/usr/bin/env bash
# WS-CFG-004 CI test: proves values.schema.json actually enforces the
# dbt/Flink/DQ/UCS integration contract, rather than just existing.
#
# For each integration this asserts:
#   1. The chart's own defaults lint/template cleanly (the contract doesn't
#      break the common case).
#   2. Enabling it with a missing url, or a missing/incomplete
#      internalTokenSecretRef, makes `helm lint` / `helm template` FAIL.
#   3. Leaving it disabled (or, for the always-on three, explicitly disabling
#      it) with an empty url/secretRef still PASSES — disabled means exempt.
#
# Run from anywhere; paths are resolved relative to this script.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHART_DIR="$(dirname "$SCRIPT_DIR")"
FAILURES=0

pass() { echo "  PASS: $1"; }
fail() { echo "  FAIL: $1"; FAILURES=$((FAILURES + 1)); }

# expect_pass/expect_fail run `helm lint` (schema validation applies equally
# to `helm template`/`helm install`) and check the exit code matches what the
# case name implies.
expect_pass() {
  local desc="$1"; shift
  if helm lint "$CHART_DIR" --set tenant.orgId=acme "$@" >/tmp/ws-cfg-004-out.log 2>&1; then
    pass "$desc"
  else
    fail "$desc (expected helm lint to succeed, it did not)"
    sed 's/^/    /' /tmp/ws-cfg-004-out.log
  fi
}

expect_fail() {
  local desc="$1"; shift
  if helm lint "$CHART_DIR" --set tenant.orgId=acme "$@" >/tmp/ws-cfg-004-out.log 2>&1; then
    fail "$desc (expected helm lint to fail, it succeeded)"
  else
    pass "$desc"
  fi
}

echo "== Baseline: chart defaults must lint cleanly =="
expect_pass "default values pass schema validation"

echo ""
echo "== Each enabled integration must require a non-empty url =="
expect_fail "dbtRunner enabled with empty url fails"      --set dbtRunner.url=""
expect_fail "flinkSqlRunner enabled with empty url fails" --set flinkSqlRunner.url=""
expect_fail "dq enabled with empty url fails"             --set dq.url=""
expect_fail "ucs enabled with empty url fails" \
  --set ucs.enabled=true --set ucs.internalTokenSecretRef.name=x --set ucs.internalTokenSecretRef.key=y --set ucs.url=""

echo ""
echo "== Each enabled integration must require a complete internalTokenSecretRef =="
expect_fail "dbtRunner enabled with missing secretRef.name fails" \
  --set dbtRunner.internalTokenSecretRef.name=""
expect_fail "flinkSqlRunner enabled with missing secretRef.key fails" \
  --set flinkSqlRunner.internalTokenSecretRef.key=""
# Note: `--set-json 'x={}'` does NOT clear an already-populated map — Helm CLI
# overrides merge into map values rather than replace them — so these clear
# each sub-field individually instead.
expect_fail "dq enabled with both secretRef fields blanked fails" \
  --set dq.internalTokenSecretRef.name="" --set dq.internalTokenSecretRef.key=""
expect_fail "ucs enabled with an incomplete secretRef (key only) fails" \
  --set ucs.url=http://ucs.example --set ucs.internalTokenSecretRef.name=""

echo ""
echo "== A disabled integration is exempt from url/secretRef requirements =="
expect_fail "ucs is REQUIRED by default (production-governed profile): disabling it fails" --set ucs.enabled=false
expect_fail "ungoverned profile without the acknowledgement fails" --set governance.profile=development-ungoverned --set ucs.enabled=false
expect_pass "development-ungoverned + acknowledgement may run without ucs (unsupported for production)" \
  --set governance.profile=development-ungoverned --set governance.acknowledgeUnsupportedForProduction=true --set ucs.enabled=false
expect_fail "production-governed refuses dataPolicy.mode=off" --set dataPolicy.mode=off
expect_fail "an unknown governance profile fails" --set governance.profile=staging-ish
expect_pass "explicitly disabling dbtRunner with empty url/secretRef still passes" \
  --set dbtRunner.enabled=false --set dbtRunner.url="" \
  --set dbtRunner.internalTokenSecretRef.name="" --set dbtRunner.internalTokenSecretRef.key=""

echo ""
echo "== Fully wiring ucs (the opt-in fourth integration) passes =="
expect_pass "ucs enabled + url + secretRef all set" \
  --set ucs.enabled=true --set ucs.url=http://ucs.example \
  --set ucs.internalTokenSecretRef.name=ucs-token --set ucs.internalTokenSecretRef.key=internal-token

echo ""
echo "== Secrets vault must be a cloud secret manager (WS-SEC-002) =="
expect_fail "secrets.backend=local fails"  --set secrets.backend=local
expect_fail "unknown secrets.backend fails" --set secrets.backend=vault
expect_fail "azure_key_vault without azureVaultUrl fails" --set secrets.backend=azure_key_vault
expect_pass "azure_key_vault with azureVaultUrl passes" \
  --set secrets.backend=azure_key_vault --set secrets.azureVaultUrl=https://kv.vault.azure.net
expect_fail "gcp_secret_manager without gcpProjectId fails" --set secrets.backend=gcp_secret_manager
expect_pass "gcp_secret_manager with gcpProjectId passes" \
  --set secrets.backend=gcp_secret_manager --set secrets.gcpProjectId=acme-prod

echo ""
echo "== Worker and egress (WS-EXEC-005 / WS-009) =="
expect_fail "zero worker replicas fails" --set worker.replicas=0
expect_fail "zero worker concurrency fails" --set worker.concurrency=0
expect_fail "malformed connection CIDR fails" --set 'egress.connectionAllowedNetworks={not-a-cidr}'
expect_pass "valid connection CIDRs pass" --set 'egress.connectionAllowedNetworks={10.20.0.0/16,fd00::/8}'
expect_pass "NetworkPolicy can be disabled" --set networkPolicy.enabled=false

echo ""
echo "== Gateway trust contract (W-001) =="
# Workspace trusts identity headers only from a gateway that presents a shared
# secret. The chart must declare that contract and refuse to render without it.
expect_fail "gateway.mode other than trusted-envoy fails"  --set gateway.mode=direct
expect_fail "gateway secretRef with no name fails"         --set gateway.internalSecretRef.name=""
expect_fail "gateway secretRef with no key fails"          --set gateway.internalSecretRef.key=""
expect_pass "a custom gateway secretRef passes" \
  --set gateway.internalSecretRef.name=my-gw --set gateway.internalSecretRef.key=token

echo ""
echo "== DQ scoped-JWT signing secret (W-002) =="
# Outside dev the legacy DQ token is never sent; the signing secret is the only credential.
expect_fail "dq enabled with no serviceJwtSecretRef.name fails" --set dq.serviceJwtSecretRef.name=""
expect_fail "dq enabled with no serviceJwtSecretRef.key fails"  --set dq.serviceJwtSecretRef.key=""
expect_fail "dq enabled with no serviceJwtSecretRef at all fails" --set dq.serviceJwtSecretRef=null
expect_pass "dq disabled needs no serviceJwtSecretRef" --set dq.enabled=false --set dq.serviceJwtSecretRef=null
expect_pass "a custom serviceJwtSecretRef passes" \
  --set dq.serviceJwtSecretRef.name=my-dq-jwt --set dq.serviceJwtSecretRef.key=k

echo ""
echo "== Delegated-token signing keys for the runners (X-001) =="
# Workspace presents a short-lived delegated JWT to each runner instead of the user's own credential.
expect_fail "dbtRunner enabled with no delegationJwtSecretRef.name fails"  --set dbtRunner.delegationJwtSecretRef.name=""
expect_fail "dbtRunner enabled with no delegationJwtSecretRef.key fails"   --set dbtRunner.delegationJwtSecretRef.key=""
expect_fail "flinkSqlRunner enabled with no delegationJwtSecretRef fails"  --set flinkSqlRunner.delegationJwtSecretRef=null
expect_fail "dq enabled with no delegationJwtSecretRef fails" --set dq.delegationJwtSecretRef=null
expect_fail "dq enabled with no delegationJwtSecretRef.name fails" --set dq.delegationJwtSecretRef.name=""
expect_pass "disabled runners need no delegation secret" \
  --set dbtRunner.enabled=false --set dbtRunner.delegationJwtSecretRef=null \
  --set flinkSqlRunner.enabled=false --set flinkSqlRunner.delegationJwtSecretRef=null

echo ""
echo "== Rendered env is what the app actually reads =="
# These regressed silently before: the app booted with SECRETS_BACKEND
# defaulting to "local" (refused in prod), and with the literal text
# "$(DATABASE_PASSWORD)" as its DB password (no expansion in envFrom).
RENDERED="$(helm template ws "$CHART_DIR" --set tenant.orgId=acme)"
expect_rendered() {
  local desc="$1" pattern="$2"
  if grep -Eq -- "$pattern" <<<"$RENDERED"; then pass "$desc"; else fail "$desc"; fi
}
expect_not_rendered() {
  local desc="$1" pattern="$2"
  if grep -Eq -- "$pattern" <<<"$RENDERED"; then fail "$desc"; else pass "$desc"; fi
}
expect_rendered     "SECRETS_BACKEND is set to a cloud backend" 'SECRETS_BACKEND: "(aws_secrets_manager|azure_key_vault|gcp_secret_manager)"'
expect_not_rendered "no unexpandable \$(VAR) placeholder in any value" '\$\([A-Z_]+\)'
expect_rendered     "DATABASE_PASSWORD comes from a secretKeyRef" 'name: DATABASE_PASSWORD'
expect_rendered     "a separate execution worker Deployment" 'command: \["python", "-m", "app.worker"\]'
expect_rendered     "a NetworkPolicy" 'kind: NetworkPolicy'
expect_rendered     "gateway identity headers are trusted" 'TRUST_ENVOY_HEADERS: "true"'
expect_rendered     "the gateway secret comes from a secretKeyRef" 'name: ENVOY_INTERNAL_SECRET'
expect_rendered     "the DQ scoped-JWT signing secret is mounted" 'name: DQ_SERVICE_JWT_SECRET'
expect_rendered     "the dbt Runner delegation key is mounted" 'name: DBTRUNNER_DELEGATION_JWT_SECRET'
expect_rendered     "the FlinkSQL Runner delegation key is mounted" 'name: FLINKSQL_DELEGATION_JWT_SECRET'
expect_rendered     "the DQ delegation key is mounted" 'name: DQ_DELEGATION_JWT_SECRET'
expect_not_rendered "no delegation key is ever a plain value" '_DELEGATION_JWT_SECRET: '
expect_not_rendered "the DQ signing secret is never a plain value" 'DQ_SERVICE_JWT_SECRET: '
expect_not_rendered "the gateway secret is never a plain value" 'ENVOY_INTERNAL_SECRET: '
expect_rendered     "the data policy mode is rendered (audit by default)" 'DATA_POLICY_MODE: "audit"'
expect_not_rendered "no libpq sslmode in the asyncpg URL" 'sslmode='
# (Exhaustive key-by-key checks against Settings: tests/test_helm_contract.py.)

echo ""
if [ "$FAILURES" -eq 0 ]; then
  echo "All chart contract tests passed."
  exit 0
else
  echo "$FAILURES chart contract test(s) failed."
  exit 1
fi
