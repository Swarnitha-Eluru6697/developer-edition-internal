"""Helm chart (docker/helm/flinksql-runner): renders + lints with defaults
and with integrations enabled; missing/placeholder required values fail;
the hardening and the RUN-FLINK-010/016 wiring are actually rendered.

Needs a `helm` binary on PATH (or HELM_BIN); skipped otherwise.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest
import yaml

CHART = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "docker", "helm", "flinksql-runner")
HELM = os.environ.get("HELM_BIN") or shutil.which("helm")
pytestmark = pytest.mark.skipif(not HELM, reason="helm binary not available")

INTEGRATIONS = [
    "ucs.enabled=true", "ucs.url=http://ucs:8000", "ucs.internalTokenSecretRef.name=ucs-tok",
    "ucs.internalTokenSecretRef.key=token", "quality.consumerEnabled=true",
    "kafka.restProxy.url=https://rest-proxy:8443", "kafka.tls.caSecretRef.name=kafka-ca",
    "kafka.schemaRegistry.authEnabled=true", "kafka.schemaRegistry.credentialsSecretRef.name=sr",
    "kafka.schemaRegistry.tls.caSecretRef.name=sr-ca", "kafka.topicAllowedPatterns={acme-42.*}",
    "kafka.restProxy.credentialsSecretRef.name=rp-creds",
]


def _helm(*args, sets=()):
    cmd = [HELM, *args, CHART]
    for s in sets:
        cmd += ["--set", s]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120)


def _render(*sets):
    r = _helm("template", "t", sets=sets)
    assert r.returncode == 0, r.stderr
    return [d for d in yaml.safe_load_all(r.stdout) if d]


def _by_kind(docs, kind):
    return [d for d in docs if d["kind"] == kind]


def test_render_without_org_id_fails():
    r = _helm("template", "t")
    assert r.returncode != 0 and "tenant.orgId is required" in r.stderr


@pytest.mark.parametrize("org", ["REPLACE_ME", "local-dev"])
def test_render_with_placeholder_org_id_fails(org):
    r = _helm("template", "t", sets=[f"tenant.orgId={org}"])
    assert r.returncode != 0 and "placeholder" in r.stderr


@pytest.mark.parametrize("missing,expected", [
    ("database.passwordSecretRef.name=", "database.passwordSecretRef.name is required"),
    ("kafka.bootstrapServers=", "kafka.bootstrapServers is required"),
    ("internalServiceToken.secretRef.name=", "internalServiceToken.secretRef.name is required"),
    ("kafka.credentialsSecretRef.name=", "kafka.credentialsSecretRef.name is required"),
    ("environment=production", "environment must be dev, staging or prod"),
])
def test_render_with_missing_mandatory_values_fails(missing, expected):
    r = _helm("template", "t", sets=["tenant.orgId=acme-42", missing])
    assert r.returncode != 0 and expected in r.stderr


def test_quality_consumer_without_ucs_fails():
    r = _helm("template", "t", sets=["tenant.orgId=acme-42", "quality.consumerEnabled=true"])
    assert r.returncode != 0 and "requires ucs.enabled=true" in r.stderr


def test_ucs_enabled_without_url_fails():
    r = _helm("template", "t", sets=["tenant.orgId=acme-42", "ucs.enabled=true"])
    assert r.returncode != 0 and "ucs.url is required" in r.stderr


@pytest.mark.parametrize("sets", [["tenant.orgId=acme-42"], ["tenant.orgId=acme-42", *INTEGRATIONS]])
def test_lint_passes(sets):
    r = _helm("lint", sets=sets)
    assert r.returncode == 0, r.stdout + r.stderr


def test_default_render_contains_every_hardening_resource():
    docs = _render("tenant.orgId=acme-42")
    kinds = {d["kind"] for d in docs}
    assert {"Deployment", "ConfigMap", "Service", "ServiceAccount", "NetworkPolicy",
            "PodDisruptionBudget", "HorizontalPodAutoscaler", "Job"} <= kinds

    dep = _by_kind(docs, "Deployment")[0]
    spec = dep["spec"]
    assert "replicas" not in spec          # HPA owns replica count
    pod = spec["template"]["spec"]
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["runAsUser"] == 9999
    assert pod["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
    assert pod["serviceAccountName"] == "flinksql-runner-acme-42"
    assert pod["automountServiceAccountToken"] is False
    assert pod["topologySpreadConstraints"]
    c = pod["containers"][0]
    sc = c["securityContext"]
    assert sc["readOnlyRootFilesystem"] is True and sc["allowPrivilegeEscalation"] is False
    assert sc["capabilities"]["drop"] == ["ALL"]
    assert {"name": "tmp", "mountPath": "/tmp"} in c["volumeMounts"]
    assert any(v["name"] == "tmp" and "emptyDir" in v for v in pod["volumes"])
    assert c["readinessProbe"]["httpGet"]["path"] == "/readyz"
    assert c["command"][0] == "uvicorn"

    hpa = _by_kind(docs, "HorizontalPodAutoscaler")[0]
    assert (hpa["spec"]["minReplicas"], hpa["spec"]["maxReplicas"]) == (2, 6)
    assert hpa["spec"]["scaleTargetRef"]["name"] == dep["metadata"]["name"]
    assert _by_kind(docs, "PodDisruptionBudget")[0]["spec"]["minAvailable"] == 1

    job = _by_kind(docs, "Job")[0]
    assert job["metadata"]["annotations"]["helm.sh/hook"] == "pre-install,pre-upgrade"
    assert job["spec"]["template"]["spec"]["containers"][0]["command"] == ["alembic", "upgrade", "head"]


def test_network_policy_allows_only_the_declared_dependencies():
    docs = _render("tenant.orgId=acme-42")
    np = _by_kind(docs, "NetworkPolicy")[0]["spec"]
    assert set(np["policyTypes"]) == {"Ingress", "Egress"}
    ports = sorted({p["port"] for rule in np["egress"] for p in rule["ports"]})
    for needed in (53, 8083, 8081, 9092, 8082, 8181, 8000, 5432, 6379, 443):
        assert needed in ports


def test_configmap_wires_environment_alerts_and_kafka_settings():
    cm = _by_kind(_render("tenant.orgId=acme-42"), "ConfigMap")[0]["data"]
    assert cm["ENVIRONMENT"] == "prod"
    assert cm["ORG_ID"] == "acme-42"
    for key, val in {"ALERT_RESTART_THRESHOLD": "3", "ALERT_RESTART_WINDOW_MINUTES": "10",
                     "ALERT_CHECKPOINT_FAILURE_WINDOW_MINUTES": "30",
                     "ALERT_BACKPRESSURE_WINDOW_MINUTES": "5", "ALERT_KAFKA_LAG_WINDOW_MINUTES": "5",
                     "ALERT_KAFKA_LAG_THRESHOLD": "100000", "ALERT_BACKPRESSURE_THRESHOLD": "0.8",
                     "QUALITY_CONSUMER_ENABLED": "false",
                     "LEGACY_INTERNAL_TOKEN_ENABLED": "false"}.items():
        assert cm[key] == val, key
    assert "KAFKA_REST_PROXY_URL" not in cm


def test_kafka_credentials_use_the_names_the_app_reads():
    dep = _by_kind(_render("tenant.orgId=acme-42"), "Deployment")[0]
    env = {e["name"] for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert {"KAFKA_SASL_USERNAME", "KAFKA_SASL_PASSWORD"} <= env
    assert not ({"KAFKA_USERNAME", "KAFKA_PASSWORD"} & env)


def test_render_with_integrations_enabled():
    docs = _render("tenant.orgId=acme-42", *INTEGRATIONS)
    cm = _by_kind(docs, "ConfigMap")[0]["data"]
    assert cm["UCS_URL"] == "http://ucs:8000"
    assert cm["QUALITY_CONSUMER_ENABLED"] == "true"
    assert cm["KAFKA_REST_PROXY_URL"] == "https://rest-proxy:8443"
    assert cm["KAFKA_SSL_CAFILE"] == cm["KAFKA_REST_PROXY_CA_FILE"] == "/etc/inaiera/tls/kafka/ca.crt"
    assert cm["SCHEMA_REGISTRY_AUTH_ENABLED"] == "true"
    assert cm["SCHEMA_REGISTRY_CA_FILE"] == "/etc/inaiera/tls/schema-registry/ca.crt"
    assert json.loads(cm["KAFKA_TOPIC_ALLOWED_PATTERNS"]) == ["acme-42.*"]
    pod = _by_kind(docs, "Deployment")[0]["spec"]["template"]["spec"]
    env = {e["name"]: e for e in pod["containers"][0]["env"]}
    assert env["UCS_INTERNAL_TOKEN"]["valueFrom"]["secretKeyRef"] == {"name": "ucs-tok", "key": "token"}
    assert {"SCHEMA_REGISTRY_USERNAME", "SCHEMA_REGISTRY_PASSWORD",
            "KAFKA_REST_PROXY_USERNAME", "KAFKA_REST_PROXY_PASSWORD"} <= set(env)
    assert {v["name"] for v in pod["volumes"]} >= {"tmp", "kafka-ca", "schema-registry-ca"}
    assert "UCS_INTERNAL_TOKEN" not in cm   # never a secret in a ConfigMap


def test_autoscaling_disabled_renders_fixed_replicas_and_no_hpa():
    docs = _render("tenant.orgId=acme-42", "autoscaling.enabled=false", "replicas=3")
    assert _by_kind(docs, "HorizontalPodAutoscaler") == []
    assert _by_kind(docs, "Deployment")[0]["spec"]["replicas"] == 3


# ── FL-001: the governed pg_lake connection comes from a Secret, never from SQL or plain values ──
PG = ["pgLake.enabled=true", "pgLake.jdbcBaseUrl=jdbc:postgresql://pg:5432", "pgLake.database=analytics",
      "pgLake.credentialsSecretRef.name=pg-lake-creds"]


def _env(docs):
    (dep,) = _by_kind(docs, "Deployment")
    return {e["name"]: e for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]}


def _cm(docs):
    return next(d for d in _by_kind(docs, "ConfigMap") if "POLARIS_REST_URI" in d.get("data", {}))["data"]


def test_pg_lake_is_off_by_default_and_adds_nothing():
    docs = _render("tenant.orgId=acme-42")
    assert "PG_LAKE_PASSWORD" not in _env(docs) and "PG_LAKE_JDBC_BASE_URL" not in _cm(docs)


def test_pg_lake_credentials_come_from_a_secret_the_url_and_database_from_the_configmap():
    docs = _render("tenant.orgId=acme-42", *PG)
    env = _env(docs)
    for name, key in (("PG_LAKE_USER", "username"), ("PG_LAKE_PASSWORD", "password")):
        ref = env[name]["valueFrom"]["secretKeyRef"]
        assert (ref["name"], ref["key"]) == ("pg-lake-creds", key) and "value" not in env[name]
    data = _cm(docs)
    assert data["PG_LAKE_JDBC_BASE_URL"] == "jdbc:postgresql://pg:5432" and data["PG_LAKE_DATABASE"] == "analytics"
    assert data["PG_LAKE_CATALOG_NAME"] == "pg_lake"


def test_no_password_value_ever_appears_in_a_rendered_manifest():
    r = _helm("template", "t", sets=["tenant.orgId=acme-42", *PG, "pgLake.credentialsSecretRef.passwordKey=pw"])
    assert r.returncode == 0, r.stderr
    assert "PG_LAKE_PASSWORD" in r.stdout and "password:" not in r.stdout.lower().replace("passwordkey", "")


@pytest.mark.parametrize("drop", ["pgLake.jdbcBaseUrl", "pgLake.database", "pgLake.credentialsSecretRef.name"])
def test_enabling_pg_lake_without_its_settings_fails_the_render(drop):
    sets = ["tenant.orgId=acme-42", *[s for s in PG if not s.startswith(drop + "=")]]
    r = _helm("template", "t", sets=sets)
    assert r.returncode != 0 and drop.split(".")[-1] in r.stderr


def test_the_outbox_delivery_settings_reach_the_app_and_match_its_defaults():
    """FL-004/FL-005: every QUALITY_OUTBOX_* the chart sets is a real setting, and the chart's defaults are the app's."""
    from app.core.config import Settings

    docs = _render("tenant.orgId=acme-42", *INTEGRATIONS)
    env = {k: v for d in _by_kind(docs, "ConfigMap") for k, v in (d.get("data") or {}).items()
           if k.startswith("QUALITY_OUTBOX_")}
    assert sorted(env) == ["QUALITY_OUTBOX_LEASE_SECONDS", "QUALITY_OUTBOX_MAX_ATTEMPTS",
                           "QUALITY_OUTBOX_RETRY_BASE_SECONDS", "QUALITY_OUTBOX_RETRY_MAX_SECONDS"]
    app_defaults = Settings(org_id="acme-42")
    for name, value in env.items():
        assert float(value) == float(getattr(app_defaults, name.lower())), name
