"""FL-008: one credential-ownership model (CREDENTIAL_OWNERSHIP.md), checked on the charts and the code.

The Flink runtime owns the secrets jobs use for Kafka / Schema Registry / Polaris; job SQL only names governed objects; the Runner holds only
the credentials ITS OWN code reads. NOT covered here (needs a secured Kafka / Schema Registry / Polaris, which this environment does not
have): that a secured cluster actually accepts the jobs — see the doc's "Known gap".
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from app.core.config import Settings
from app.services import pg_lake
from app.services.job_service import _inject_catalog_config

ROOT = Path(__file__).resolve().parent.parent
RUNNER_CHART = ROOT / "docker" / "helm" / "flinksql-runner"
CLUSTER_CHART = ROOT / "docker" / "helm" / "flink-cluster"
HELM = os.environ.get("HELM_BIN") or shutil.which("helm")
needs_helm = pytest.mark.skipif(not HELM, reason="helm binary not available")

SENTINELS = {"kafka": "KAFKA-SENTINEL-pw", "sr": "SR-SENTINEL-pw", "polaris": "POLARIS-SENTINEL-secret"}


def _render_runner(*sets: str) -> list[dict]:
    cmd = [HELM, "template", "t", str(RUNNER_CHART), "--set", "tenant.orgId=acme-42"]
    for s in sets:
        cmd += ["--set", s]
    done = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
    assert done.returncode == 0, done.stderr
    return [d for d in yaml.safe_load_all(done.stdout) if d]


def _secret_env(docs) -> dict[str, dict]:
    (dep,) = [d for d in docs if d["kind"] == "Deployment"]
    env = dep["spec"]["template"]["spec"]["containers"][0]["env"]
    return {e["name"]: e["valueFrom"]["secretKeyRef"] for e in env if "secretKeyRef" in e.get("valueFrom", {})}


def _field_for(env_name: str) -> str | None:
    for name, field in Settings.model_fields.items():
        aliases = field.validation_alias
        choices = [a for a in getattr(aliases, "choices", [])] if aliases is not None else []
        if env_name.lower() == name or env_name in choices or env_name.lower() in choices:
            return name
    return None


@needs_helm
def test_the_runner_pod_mounts_no_polaris_credential():
    docs = _render_runner("polaris.restUri=http://polaris:8181/api/catalog")
    assert not [n for n in _secret_env(docs) if "POLARIS" in n]
    for d in docs:                                                    # ... and none leaks into any ConfigMap either
        assert not [k for k in (d.get("data") or {}) if "POLARIS" in k and ("SECRET" in k or "CLIENT" in k)]


@needs_helm
def test_every_secret_the_runner_pod_mounts_is_read_by_the_runners_code():
    """'Mounted but never read' is how a chart looks configured while the credential belongs to someone else. Every Secret-backed
    env var of the Runner maps to a Settings field that code outside config.py actually uses."""
    docs = _render_runner("kafka.schemaRegistry.authEnabled=true", "kafka.schemaRegistry.credentialsSecretRef.name=sr",
                          "kafka.restProxy.url=https://rest-proxy:8443", "kafka.restProxy.credentialsSecretRef.name=rp",
                          "ucs.enabled=true", "ucs.url=http://ucs:8000", "ucs.internalTokenSecretRef.name=ucs-tok",
                          "ucs.internalTokenSecretRef.key=token", "quality.consumerEnabled=true",
                          "pgLake.enabled=true", "pgLake.jdbcBaseUrl=jdbc:postgresql://pg:5432", "pgLake.database=lake",
                          "pgLake.credentialsSecretRef.name=pgl")
    source = "\n".join(p.read_text() for p in (ROOT / "app").rglob("*.py") if p.name != "config.py")
    (dep,) = [d for d in docs if d["kind"] == "Deployment"]
    interpolated = {m for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]
                    for m in re.findall(r"\$\(([A-Z_]+)\)", str(e.get("value", "")))}     # e.g. DATABASE_URL embeds $(DATABASE_PASSWORD)
    unread = []
    for env_name in _secret_env(docs):
        if env_name in interpolated:
            continue
        field = _field_for(env_name)
        assert field, f"{env_name}: the chart injects it but Settings has no such field"
        if not re.search(rf"\b{field}\b", source):
            unread.append(f"{env_name} -> settings.{field}")
    assert unread == [], f"mounted into the Runner but never read by its code: {unread}"


@needs_helm
def test_the_runner_chart_keeps_the_polaris_address_but_no_credential_values():
    values = yaml.safe_load((RUNNER_CHART / "values.yaml").read_text())
    assert set(values["polaris"]) == {"restUri", "icebergWarehouse"}


def test_the_session_catalog_config_carries_no_credential(monkeypatch):
    from app.core.config import get_settings
    s = get_settings()
    for field, value in (("kafka_sasl_password", SENTINELS["kafka"]), ("schema_registry_password", SENTINELS["sr"]),
                         ("kafka_sasl_username", "svc-user"), ("schema_registry_username", "sr-user")):
        monkeypatch.setattr(s, field, value, raising=False)
    sql = _inject_catalog_config("INSERT INTO t SELECT 1;", "trino", "s3://bucket/sp-1")
    assert "table.catalog.inaiera.uri" in sql and "table.catalog.inaiera.warehouse" in sql
    for secret in (*SENTINELS.values(), "svc-user", "sr-user"):
        assert secret not in sql
    assert not re.search(r"credential|jaas|sasl|password|secret", sql, re.IGNORECASE)


CREDENTIAL_SQL = {
    "kafka sasl jaas": "CREATE TABLE t (a INT) WITH ('connector'='kafka','properties.sasl.jaas.config'='org.apache.kafka.common.security.scram.ScramLoginModule required username=\"u\" password=\"p\";');",
    "jaas without a password word": "CREATE TABLE t (a INT) WITH ('connector'='kafka','properties.sasl.jaas.config'='x');",
    "schema registry basic auth": "CREATE TABLE t (a INT) WITH ('format'='avro-confluent','avro-confluent.basic-auth.user-info'='u:p');",
    "polaris credential": "CREATE CATALOG c WITH ('type'='iceberg','catalog-impl'='org.apache.iceberg.rest.RESTCatalog','credential'='id:secret');",
    "polaris token": "CREATE CATALOG c WITH ('type'='iceberg','token'='abc');",
    "keystore password": "CREATE TABLE t (a INT) WITH ('properties.ssl.keystore.password'='x');",
}


@pytest.mark.parametrize("name", sorted(CREDENTIAL_SQL))
def test_job_sql_that_carries_a_connector_credential_is_refused(name):
    problems = pg_lake.sql_problems(CREDENTIAL_SQL[name], Settings())
    assert problems and "credential" in problems[0], name


def test_job_sql_that_names_governed_objects_is_accepted():
    sql = ("CREATE TABLE t (a INT) WITH ('connector'='kafka','topic'='acme.orders','properties.security.protocol'='SASL_SSL',"
           "'properties.sasl.mechanism'='SCRAM-SHA-512','format'='avro-confluent');\nINSERT INTO inaiera.db.sink SELECT * FROM t;")
    assert pg_lake.sql_problems(sql, Settings()) == []


def test_the_flink_cluster_chart_owns_the_connector_secrets():
    """The model's other half: the SQL Gateway and the TaskManagers are where the Kafka and Polaris Secrets are mounted. (Textual: this
    chart depends on the Flink operator chart, which needs the network to build.)"""
    gateway = (CLUSTER_CHART / "templates" / "sql-gateway.yaml").read_text()
    deployment = (CLUSTER_CHART / "templates" / "flink-deployment.yaml").read_text()
    for needle in ("KAFKA_USERNAME", "KAFKA_PASSWORD", "POLARIS_CLIENT_ID", "POLARIS_CLIENT_SECRET"):
        assert needle in gateway, f"SQL Gateway does not mount {needle}"
    assert "POLARIS_CLIENT_SECRET" in deployment


def test_the_ownership_doc_names_both_owners_and_the_open_gap():
    doc = (ROOT / "CREDENTIAL_OWNERSHIP.md").read_text()
    assert "Flink runtime owns" in doc and "Known gap" in doc and "NOT done" in doc
