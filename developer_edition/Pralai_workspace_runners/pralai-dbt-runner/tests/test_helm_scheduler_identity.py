"""DBT-003: the dbt Runner chart wires Dagster's own scheduler identity (SCHEDULER_JWT_SECRET).

Needs `helm` on PATH (skipped otherwise); no app imports.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).resolve().parent.parent / "docker" / "helm" / "dbtrunner"
pytestmark = pytest.mark.skipif(not shutil.which("helm"), reason="helm not installed")


def _template(*extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(["helm", "template", "r", str(CHART), "--set", "tenant.orgId=acme", "-f", str(CHART / "ci" / "egress-values.yaml"), *extra],
                          capture_output=True, text=True, check=False)


def _docs(*extra):
    done = _template(*extra)
    assert done.returncode == 0, done.stderr
    return [d for d in yaml.safe_load_all(done.stdout) if d]


def _api_env(docs) -> dict:
    (dep,) = [d for d in docs if d["kind"] == "Deployment"]
    return {e["name"]: e for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]}


def test_the_scheduler_secret_comes_from_a_secret_never_the_configmap():
    docs = _docs()
    (cm,) = [d for d in docs if d["kind"] == "ConfigMap"]
    assert "SCHEDULER_JWT_SECRET" not in cm["data"]
    env = _api_env(docs)["SCHEDULER_JWT_SECRET"]
    assert "value" not in env
    assert env["valueFrom"]["secretKeyRef"] == {"name": "dbtrunner-scheduler-jwt", "key": "secret"}


def test_the_scheduler_secret_reference_is_configurable():
    env = _api_env(_docs("--set", "schedulerJwt.secretRef.name=acme-sched", "--set", "schedulerJwt.secretRef.key=k"))
    assert env["SCHEDULER_JWT_SECRET"]["valueFrom"]["secretKeyRef"] == {"name": "acme-sched", "key": "k"}


def test_the_scheduler_issuer_allowlist_is_rendered():
    (cm,) = [d for d in _docs() if d["kind"] == "ConfigMap"]
    assert cm["data"]["SCHEDULER_JWT_ALLOWED_ISSUERS"] == '["dagster"]'


def test_it_is_its_own_secret_not_the_internal_token_or_another_identitys():
    env = _api_env(_docs())
    refs = {n: (env[n]["valueFrom"]["secretKeyRef"]["name"], env[n]["valueFrom"]["secretKeyRef"]["key"])
            for n in ("SCHEDULER_JWT_SECRET", "INTERNAL_SERVICE_TOKEN", "DELEGATION_JWT_SECRET")}
    assert len(set(refs.values())) == 3, refs


@pytest.mark.parametrize("overrides", [
    ["schedulerJwt.secretRef.name="], ["schedulerJwt.secretRef.key="],
], ids=["no-name", "no-key"])
def test_the_chart_refuses_an_enabled_scheduler_identity_without_its_secret(overrides):
    done = _template("--set", overrides[0])
    assert done.returncode != 0
    assert "schedulerJwt.secretRef" in done.stderr


def test_the_chart_refuses_reusing_the_internal_token_secret_as_the_scheduler_key():
    done = _template("--set", "schedulerJwt.secretRef.name=dbtrunner-internal-token", "--set", "schedulerJwt.secretRef.key=token")
    assert done.returncode != 0
    assert "internalServiceToken" in done.stderr


def test_disabled_scheduler_identity_mounts_nothing_and_needs_no_secret():
    docs = _docs("--set", "schedulerJwt.enabled=false", "--set", "schedulerJwt.secretRef.name=")
    assert "SCHEDULER_JWT_SECRET" not in _api_env(docs)
