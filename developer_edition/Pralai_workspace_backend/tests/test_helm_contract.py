"""The Helm chart's rendered env matches what the app actually reads.

Every key the chart puts in the ConfigMap or a container's env must be a
Settings field (or one of the few env vars read directly), so a misspelt or
renamed key — JUPYTERHUB_API_URL, the libpq `sslmode=` DATABASE_URL, the
unused TELEMETRY_BATCH_SIZE — fails CI instead of being silently ignored at
runtime. Also asserts the deployment shape the review requires (separate
worker, hardened pods, NetworkPolicy, a migrate Job that doesn't need the
ConfigMap).

Renders with `helm` when it's on PATH, or reads HELM_RENDERED_FILE; skipped
otherwise.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from app.core.config import Settings

CHART = Path(__file__).resolve().parent.parent / "docker" / "helm" / "workspace"
# Read straight from os.environ, not via Settings.
DIRECT_ENV = {"TRUST_ENVOY_HEADERS", "ENVOY_INTERNAL_SECRET", "WORKSPACE_GIT_ROOT", "WORKER_HEARTBEAT_PATH"}


def _helm_template(*extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["helm", "template", "ws", str(CHART), "--set", "tenant.orgId=acme", *extra],
        capture_output=True, text=True, check=False,
    )


def _render() -> list[dict]:
    path = os.environ.get("HELM_RENDERED_FILE")
    if path:
        text = Path(path).read_text()
    elif shutil.which("helm"):
        done = _helm_template()
        assert done.returncode == 0, done.stderr
        text = done.stdout
    else:
        pytest.skip("helm not installed and HELM_RENDERED_FILE not set")
    return [d for d in yaml.safe_load_all(text) if d]


@pytest.fixture(scope="module")
def docs():
    return _render()


def _by_kind(docs, kind):
    return [d for d in docs if d.get("kind") == kind]


def _deployment(docs, component):
    for d in _by_kind(docs, "Deployment"):
        if d["metadata"]["labels"].get("app.kubernetes.io/component") == component:
            return d
    raise AssertionError(f"no {component} Deployment")


def _known_keys() -> set[str]:
    return {name.upper() for name in Settings.model_fields} | DIRECT_ENV


def test_every_configmap_key_is_read_by_the_app(docs):
    (cm,) = _by_kind(docs, "ConfigMap")
    unknown = sorted(set(cm["data"]) - _known_keys())
    assert unknown == [], f"chart sets env the app never reads: {unknown}"


def test_every_container_env_var_is_read_by_the_app(docs):
    unknown = set()
    for kind in ("Deployment", "Job"):
        for d in _by_kind(docs, kind):
            for c in d["spec"]["template"]["spec"]["containers"]:
                unknown |= {e["name"] for e in c.get("env", [])} - _known_keys()
    assert unknown == set(), f"container env the app never reads: {sorted(unknown)}"


def test_database_url_is_asyncpg_compatible(docs):
    (cm,) = _by_kind(docs, "ConfigMap")
    url = cm["data"]["DATABASE_URL"]
    assert url.startswith("postgresql+asyncpg://") and "sslmode=" not in url and "$(" not in url


def test_jupyterhub_url_is_the_hub_base(docs):
    (cm,) = _by_kind(docs, "ConfigMap")
    assert not cm["data"]["JUPYTERHUB_URL"].rstrip("/").endswith(("/hub/api", "/hub"))


def test_api_and_worker_are_separate_deployments(docs):
    api, worker = _deployment(docs, "api"), _deployment(docs, "worker")
    api_env = {e["name"]: e.get("value") for e in api["spec"]["template"]["spec"]["containers"][0]["env"]}
    w = worker["spec"]["template"]["spec"]["containers"][0]
    w_env = {e["name"]: e.get("value") for e in w["env"]}
    assert api_env["EXECUTION_WORKER_ENABLED"] == "false"
    assert w_env["EXECUTION_WORKER_ENABLED"] == "true"
    assert w["command"] == ["python", "-m", "app.worker"]
    # The API Service must never select worker pods.
    (svc,) = _by_kind(docs, "Service")
    worker_labels = worker["spec"]["template"]["metadata"]["labels"]
    assert not all(worker_labels.get(k) == v for k, v in svc["spec"]["selector"].items())


def test_migrate_job_does_not_depend_on_the_configmap(docs):
    (job,) = _by_kind(docs, "Job")
    c = job["spec"]["template"]["spec"]["containers"][0]
    assert "envFrom" not in c                      # pre-install hook: ConfigMap doesn't exist yet
    assert {e["name"] for e in c["env"]} >= {"DATABASE_URL", "DATABASE_PASSWORD"}
    (svc,) = _by_kind(docs, "Service")
    labels = job["spec"]["template"]["metadata"]["labels"]
    assert not all(labels.get(k) == v for k, v in svc["spec"]["selector"].items())


@pytest.mark.parametrize("kind", ["Deployment", "Job"])
def test_pods_are_hardened(docs, kind):
    for d in _by_kind(docs, kind):
        spec = d["spec"]["template"]["spec"]
        assert spec["automountServiceAccountToken"] is False
        assert spec["securityContext"]["runAsNonRoot"] is True
        assert spec["securityContext"]["runAsUser"] == 10001
        assert spec["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
        for c in spec["containers"]:
            sc = c["securityContext"]
            assert sc["readOnlyRootFilesystem"] is True
            assert sc["allowPrivilegeEscalation"] is False
            assert sc["capabilities"]["drop"] == ["ALL"]


def test_network_policy_default_denies_with_explicit_allows(docs):
    (np,) = _by_kind(docs, "NetworkPolicy")
    assert set(np["spec"]["policyTypes"]) == {"Ingress", "Egress"}
    sel = np["spec"]["podSelector"]["matchLabels"]
    for kind in ("Deployment", "Job"):
        for d in _by_kind(docs, kind):
            labels = d["spec"]["template"]["metadata"]["labels"]
            assert all(labels.get(k) == v for k, v in sel.items()), f"{d['metadata']['name']} not covered"
    public = [r for r in np["spec"]["egress"] if any(
        p.get("ipBlock", {}).get("cidr") == "0.0.0.0/0" for p in r.get("to", []))]
    for rule in public:     # public egress is HTTPS only, never private/metadata ranges
        assert [p["port"] for p in rule["ports"]] == [443]
        excepts = rule["to"][0]["ipBlock"]["except"]
        assert "169.254.0.0/16" in excepts and "10.0.0.0/8" in excepts


# ---- W-001: the gateway/Envoy trust contract is declared by the chart -------------------------
# The app (app/core/deps.py) trusts identity headers only when TRUST_ENVOY_HEADERS is the literal
# "true" AND the request carries the gateway's shared secret (ENVOY_INTERNAL_SECRET). A chart that
# declares neither deploys pods that look healthy and answer every protected call with 401.
def _container_env(docs, component) -> dict[str, dict]:
    c = _deployment(docs, component)["spec"]["template"]["spec"]["containers"][0]
    return {e["name"]: e for e in c["env"]}


def test_gateway_trust_is_switched_on_in_the_configmap(docs):
    (cm,) = _by_kind(docs, "ConfigMap")
    assert cm["data"]["TRUST_ENVOY_HEADERS"] == "true"      # exactly this string: the app refuses "True"/"1"


@pytest.mark.parametrize("component", ["api", "worker"])
def test_gateway_secret_comes_from_a_secret_never_the_configmap(docs, component):
    (cm,) = _by_kind(docs, "ConfigMap")
    assert "ENVOY_INTERNAL_SECRET" not in cm["data"]
    env = _container_env(docs, component)["ENVOY_INTERNAL_SECRET"]
    assert "value" not in env, "the gateway secret must not be written into the pod spec"
    assert env["valueFrom"]["secretKeyRef"] == {"name": "workspace-gateway-secret", "key": "secret"}


def test_gateway_secret_reference_is_configurable(docs):
    if not shutil.which("helm"):
        pytest.skip("needs helm to re-render with other values")
    done = _helm_template("--set", "gateway.internalSecretRef.name=acme-gw", "--set", "gateway.internalSecretRef.key=token")
    assert done.returncode == 0, done.stderr
    api = next(d for d in yaml.safe_load_all(done.stdout) if d and d.get("kind") == "Deployment"
               and d["metadata"]["labels"].get("app.kubernetes.io/component") == "api")
    env = {e["name"]: e for e in api["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["ENVOY_INTERNAL_SECRET"]["valueFrom"]["secretKeyRef"] == {"name": "acme-gw", "key": "token"}


@pytest.mark.parametrize("overrides", [
    ["--set", "gateway.internalSecretRef.name="],             # no secret name
    ["--set", "gateway.internalSecretRef.key="],              # no secret key
    ["--set", "gateway.internalSecretRef=null"],              # no secret reference at all
    ["--set", "gateway=null"],                                # no gateway section at all
    ["--set", "gateway.mode=direct"],                         # a mode the app does not have
    ["--set", "gateway.mode="],
], ids=["empty-name", "empty-key", "no-secretref", "no-gateway", "unknown-mode", "empty-mode"])
def test_chart_refuses_to_render_without_a_complete_gateway_contract(overrides):
    if not shutil.which("helm"):
        pytest.skip("needs helm")
    done = _helm_template(*overrides)
    assert done.returncode != 0, "the chart rendered without a trusted-gateway contract"
    assert "gateway" in done.stderr.lower()


# ---- W-002: the DQ scoped-JWT signing secret is mounted, and required when DQ is on -----------
# dq_proxy signs the validate call with DQ_SERVICE_JWT_SECRET; outside dev the legacy raw
# DQ_INTERNAL_TOKEN is never sent (app/core/service_tokens.py), so a pod without the signing
# secret answers /api/v1/dq/validate/rule with 503 while the DQ integration looks configured.
@pytest.mark.parametrize("component", ["api", "worker"])
def test_dq_scoped_jwt_secret_is_mounted_from_a_secret(docs, component):
    env = _container_env(docs, component)["DQ_SERVICE_JWT_SECRET"]
    assert "value" not in env, "the signing secret must not be written into the pod spec"
    assert env["valueFrom"]["secretKeyRef"] == {"name": "workspace-dq-service-jwt", "key": "secret"}


def test_dq_scoped_jwt_secret_is_not_in_the_configmap(docs):
    (cm,) = _by_kind(docs, "ConfigMap")
    assert "DQ_SERVICE_JWT_SECRET" not in cm["data"]


def test_dq_scoped_jwt_secret_reference_is_configurable():
    if not shutil.which("helm"):
        pytest.skip("needs helm to re-render with other values")
    done = _helm_template("--set", "dq.serviceJwtSecretRef.name=acme-dq-jwt", "--set", "dq.serviceJwtSecretRef.key=k")
    assert done.returncode == 0, done.stderr
    api = next(d for d in yaml.safe_load_all(done.stdout) if d and d.get("kind") == "Deployment"
               and d["metadata"]["labels"].get("app.kubernetes.io/component") == "api")
    env = {e["name"]: e for e in api["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["DQ_SERVICE_JWT_SECRET"]["valueFrom"]["secretKeyRef"] == {"name": "acme-dq-jwt", "key": "k"}


@pytest.mark.parametrize("overrides", [
    ["--set", "dq.serviceJwtSecretRef.name="],               # no secret name
    ["--set", "dq.serviceJwtSecretRef.key="],                # no secret key
    ["--set", "dq.serviceJwtSecretRef=null"],                # no reference at all
], ids=["empty-name", "empty-key", "no-secretref"])
def test_chart_refuses_dq_without_the_scoped_jwt_secret(overrides):
    if not shutil.which("helm"):
        pytest.skip("needs helm")
    done = _helm_template(*overrides)
    assert done.returncode != 0, "the chart rendered DQ without its scoped-JWT signing secret"
    assert "serviceJwtSecretRef" in done.stderr


def test_disabled_dq_needs_no_scoped_jwt_secret():
    if not shutil.which("helm"):
        pytest.skip("needs helm")
    done = _helm_template("--set", "dq.enabled=false", "--set", "dq.serviceJwtSecretRef=null")
    assert done.returncode == 0, done.stderr
    assert "DQ_SERVICE_JWT_SECRET" not in done.stdout


# ---- X-001: delegated-token signing keys for the dbt and FlinkSQL runners are mounted and required ----
# Workspace signs a short-lived delegated JWT per runner call (app/core/service_tokens.py) with these keys;
# each must equal the runner's DELEGATION_JWT_SECRET. Without one, every call to that runner is refused (503).
DELEGATION = [("DBTRUNNER_DELEGATION_JWT_SECRET", "dbtRunner", "workspace-dbtrunner-delegation-jwt"),
              ("FLINKSQL_DELEGATION_JWT_SECRET", "flinkSqlRunner", "workspace-flinksql-delegation-jwt"),
              ("DQ_DELEGATION_JWT_SECRET", "dq", "workspace-dq-delegation-jwt")]


@pytest.mark.parametrize("component", ["api", "worker"])
@pytest.mark.parametrize("env_name,_value,secret_name", DELEGATION, ids=[d[1] for d in DELEGATION])
def test_runner_delegation_secrets_are_mounted_from_a_secret(docs, component, env_name, _value, secret_name):
    env = _container_env(docs, component)[env_name]
    assert "value" not in env, "the signing secret must not be written into the pod spec"
    assert env["valueFrom"]["secretKeyRef"] == {"name": secret_name, "key": "secret"}


def test_runner_delegation_secrets_are_not_in_the_configmap(docs):
    (cm,) = _by_kind(docs, "ConfigMap")
    assert not {d[0] for d in DELEGATION} & set(cm["data"])


@pytest.mark.parametrize("env_name,values_key,_n", DELEGATION, ids=[d[1] for d in DELEGATION])
def test_runner_delegation_secret_reference_is_configurable(env_name, values_key, _n):
    if not shutil.which("helm"):
        pytest.skip("needs helm to re-render with other values")
    done = _helm_template("--set", f"{values_key}.delegationJwtSecretRef.name=acme-x", "--set", f"{values_key}.delegationJwtSecretRef.key=k")
    assert done.returncode == 0, done.stderr
    api = next(d for d in yaml.safe_load_all(done.stdout) if d and d.get("kind") == "Deployment"
               and d["metadata"]["labels"].get("app.kubernetes.io/component") == "api")
    env = {e["name"]: e for e in api["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env[env_name]["valueFrom"]["secretKeyRef"] == {"name": "acme-x", "key": "k"}


@pytest.mark.parametrize("values_key", ["dbtRunner", "flinkSqlRunner", "dq"])
@pytest.mark.parametrize("overrides", [
    ["name="], ["key="], ["=null"],
], ids=["empty-name", "empty-key", "no-secretref"])
def test_chart_refuses_a_runner_without_its_delegation_secret(values_key, overrides):
    if not shutil.which("helm"):
        pytest.skip("needs helm")
    ref = f"{values_key}.delegationJwtSecretRef"
    arg = f"{ref}{overrides[0]}" if overrides[0].startswith("=") else f"{ref}.{overrides[0]}"
    done = _helm_template("--set", arg)
    assert done.returncode != 0, "the chart rendered a runner integration with no delegation signing secret"
    assert "delegationJwtSecretRef" in done.stderr


@pytest.mark.parametrize("values_key,env_name", [("dbtRunner", "DBTRUNNER_DELEGATION_JWT_SECRET"), ("flinkSqlRunner", "FLINKSQL_DELEGATION_JWT_SECRET"),
                                                   ("dq", "DQ_DELEGATION_JWT_SECRET")])
def test_a_disabled_runner_needs_no_delegation_secret(values_key, env_name):
    if not shutil.which("helm"):
        pytest.skip("needs helm")
    done = _helm_template("--set", f"{values_key}.enabled=false", "--set", f"{values_key}.delegationJwtSecretRef=null")
    assert done.returncode == 0, done.stderr
    assert env_name not in done.stdout


# ---- X-006: the resource/data policy gate (UCS) is configurable and fails closed -----------------
def _config(done: subprocess.CompletedProcess) -> dict:
    assert done.returncode == 0, done.stderr
    cm = next(d for d in yaml.safe_load_all(done.stdout) if d and d.get("kind") == "ConfigMap" and d["metadata"]["name"].endswith("-config"))
    return cm["data"]


def test_data_policy_defaults_to_audit(docs):
    data = next(d for d in docs if d.get("kind") == "ConfigMap" and d["metadata"]["name"].endswith("-config"))["data"]
    assert data["DATA_POLICY_MODE"] == "audit" and data["DATA_POLICY_ENFORCED_ACTIONS"] == "[]"
    assert data["DATA_POLICY_CATALOG"] == "inaiera_workspace"


def test_data_policy_can_enforce_some_actions_when_ucs_is_enabled():
    if not shutil.which("helm"):
        pytest.skip("needs helm")
    data = _config(_helm_template("--set", "ucs.enabled=true", "--set", "ucs.url=http://ucs", "--set", "dataPolicy.mode=enforce",
                                  "--set", "ucs.internalTokenSecretRef.name=ucs", "--set", "ucs.internalTokenSecretRef.key=k",
                                  "--set", "dataPolicy.enforcedActions={flink.deploy,dbt.run}"))
    assert data["DATA_POLICY_MODE"] == "enforce" and data["DATA_POLICY_ENFORCED_ACTIONS"] == '["flink.deploy","dbt.run"]'


@pytest.mark.parametrize("overrides", [
    ["--set", "governance.profile=development-ungoverned", "--set", "governance.acknowledgeUnsupportedForProduction=true",
     "--set", "ucs.enabled=false", "--set", "dataPolicy.mode=enforce"],   # enforce fails closed: without UCS every governed action would be refused
    ["--set", "dataPolicy.mode=strict"],                        # not a mode
    ["--set", "dataPolicy.enforcedActions={not an action}"],    # not a product action
], ids=["enforce-without-ucs", "unknown-mode", "unknown-action"])
def test_chart_refuses_an_unusable_data_policy(overrides):
    if not shutil.which("helm"):
        pytest.skip("needs helm")
    done = _helm_template(*overrides)
    assert done.returncode != 0, "the chart rendered an unusable data policy configuration"
    assert "dataPolicy" in done.stderr or "ucs" in done.stderr.lower()


def test_data_policy_without_any_values_defaults_to_audit_never_to_enforce():
    """A values file that drops the dataPolicy block must get the safe default, not an enforcing gate that fails closed."""
    if not shutil.which("helm"):
        pytest.skip("needs helm")
    data = _config(_helm_template("--set", "dataPolicy=null"))
    assert data["DATA_POLICY_MODE"] == "audit" and data["DATA_POLICY_ENFORCED_ACTIONS"] == "[]"


# ---- W-010: explicit governance profiles ---------------------------------------------------------------
def test_the_default_profile_is_production_governed_and_requires_ucs(docs):
    cm = next(d for d in docs if d.get("kind") == "ConfigMap" and d["metadata"]["name"].endswith("-config"))["data"]
    assert cm["GOVERNANCE_PROFILE"] == "production-governed" and cm["UCS_SERVICE_URL"].startswith("http://ucs-catalog.")
    api = _deployment(docs, "api")
    env = {e["name"] for e in api["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert "UCS_GATEWAY_SECRET" in env
    assert api["metadata"]["labels"]["inaiera.com/supported-for-production"] == "true"


@pytest.mark.parametrize("overrides", [
    ["--set", "ucs.enabled=false"],
    ["--set", "ucs.url="],
    ["--set", "ucs.internalTokenSecretRef.name="],
    ["--set", "dataPolicy.mode=off"],
    ["--set", "governance.profile=development-ungoverned", "--set", "ucs.enabled=false"],       # no acknowledgement
    ["--set", "governance.profile=staging-ish"],
], ids=["no-ucs", "no-url", "no-secret", "policy-off", "ungoverned-unacknowledged", "unknown-profile"])
def test_the_production_profile_refuses_to_render_partially_governed(overrides):
    if not shutil.which("helm"):
        pytest.skip("needs helm")
    done = _helm_template(*overrides)
    assert done.returncode != 0, "the chart rendered a partially governed data plane"


def test_the_ungoverned_profile_is_labelled_unsupported_for_production():
    if not shutil.which("helm"):
        pytest.skip("needs helm")
    done = _helm_template("--set", "governance.profile=development-ungoverned", "--set", "governance.acknowledgeUnsupportedForProduction=true",
                          "--set", "ucs.enabled=false")
    assert done.returncode == 0, done.stderr
    docs = [d for d in yaml.safe_load_all(done.stdout) if d]
    assert _config_from(docs)["GOVERNANCE_PROFILE"] == "development-ungoverned"
    assert all(d["metadata"]["labels"]["inaiera.com/supported-for-production"] == "false" for d in docs
               if d["kind"] == "Deployment")
    assert "UCS_SERVICE_URL" not in _config_from(docs)


def _config_from(docs) -> dict:
    return next(d for d in docs if d.get("kind") == "ConfigMap" and d["metadata"]["name"].endswith("-config"))["data"]


# ---- W-006: the control-plane login URL is configurable -------------------------------------------------
def test_the_login_url_is_derived_unless_set(docs):
    cm = next(d for d in docs if d.get("kind") == "ConfigMap" and d["metadata"]["name"].endswith("-config"))["data"]
    assert "CONTROL_PLANE_LOGIN_URL" not in cm            # derived from CONTROL_PLANE_ISSUER by the app


def test_the_login_url_can_be_set_per_deployment():
    if not shutil.which("helm"):
        pytest.skip("needs helm")
    data = _config(_helm_template("--set", "controlPlane.loginUrl=https://sso.corp.example/enter"))
    assert data["CONTROL_PLANE_LOGIN_URL"] == "https://sso.corp.example/enter"


# ---- W-011: the connection owner bypass is an explicit policy -------------------------------------------
def test_the_owner_bypass_is_left_to_the_governance_profile_by_default(docs):
    cm = next(d for d in docs if d.get("kind") == "ConfigMap" and d["metadata"]["name"].endswith("-config"))["data"]
    assert "CONNECTION_OWNER_BYPASS" not in cm


@pytest.mark.parametrize("value", ["true", "false"])
def test_the_owner_bypass_can_be_set_explicitly(value):
    if not shutil.which("helm"):
        pytest.skip("needs helm")
    assert _config(_helm_template("--set", f"connections.ownerBypass={value}"))["CONNECTION_OWNER_BYPASS"] == value


def _config_env(*sets: str) -> dict:
    args = [a for s in sets for a in ("--set", s)]
    done = _helm_template(*args)
    assert done.returncode == 0, done.stderr
    cm = next(d for d in yaml.safe_load_all(done.stdout) if d and d.get("kind") == "ConfigMap" and "DBTRUNNER_URL" in (d.get("data") or {}))
    return cm["data"]


def test_the_dagster_url_is_not_set_by_default():
    """Without it Run now calls the runner directly: a deployment without Dagster has no dependency rule to apply."""
    if not shutil.which("helm"):
        pytest.skip("needs helm")
    assert "DAGSTER_GRAPHQL_URL" not in _config_env()


def test_the_dagster_url_reaches_the_workspace_when_set():
    if not shutil.which("helm"):
        pytest.skip("needs helm")
    url = "http://dagster-webserver.dagster.svc.cluster.local:3000/graphql"
    assert _config_env(f"dagster.graphqlUrl={url}")["DAGSTER_GRAPHQL_URL"] == url
