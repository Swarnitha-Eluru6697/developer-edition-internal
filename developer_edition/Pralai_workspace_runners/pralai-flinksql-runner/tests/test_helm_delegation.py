"""X-001: the FlinkSQL Runner chart wires the delegated-token contract.

Workspace calls the runner with a short-lived delegated JWT signed with DELEGATION_JWT_SECRET, and the runner
only accepts tokens whose audience is its own. Needs a `helm` binary (skipped otherwise); no app imports.
"""
from __future__ import annotations

import os
import shutil
import subprocess

import pytest
import yaml

CHART = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docker", "helm", "flinksql-runner")
HELM = os.environ.get("HELM_BIN") or shutil.which("helm")
pytestmark = pytest.mark.skipif(not HELM, reason="helm binary not available")


def _template(*sets: str) -> subprocess.CompletedProcess:
    cmd = [HELM, "template", "t", CHART, "--set", "tenant.orgId=acme-42"]
    for s in sets:
        cmd += ["--set", s]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)


def _docs(*sets):
    done = _template(*sets)
    assert done.returncode == 0, done.stderr
    return [d for d in yaml.safe_load_all(done.stdout) if d]


def _api_env(docs) -> dict:
    deps = [d for d in docs if d["kind"] == "Deployment"]
    assert deps
    return {e["name"]: e for e in deps[0]["spec"]["template"]["spec"]["containers"][0]["env"]}


def test_the_runner_accepts_only_its_own_audience_by_default():
    (cm,) = [d for d in _docs() if d["kind"] == "ConfigMap"]
    assert cm["data"]["CONTROL_PLANE_AUDIENCE"] == "flinksql-runner"


def test_the_delegation_secret_comes_from_a_secret_never_the_configmap():
    docs = _docs()
    (cm,) = [d for d in docs if d["kind"] == "ConfigMap"]
    assert "DELEGATION_JWT_SECRET" not in cm["data"]
    env = _api_env(docs)["DELEGATION_JWT_SECRET"]
    assert "value" not in env
    assert env["valueFrom"]["secretKeyRef"] == {"name": "flinksql-delegation-jwt", "key": "secret"}


def test_the_delegation_secret_reference_is_configurable():
    env = _api_env(_docs("delegation.secretRef.name=acme-deleg", "delegation.secretRef.key=k"))
    assert env["DELEGATION_JWT_SECRET"]["valueFrom"]["secretKeyRef"] == {"name": "acme-deleg", "key": "k"}


def test_the_delegation_issuer_allowlist_and_lifetime_cap_are_rendered():
    (cm,) = [d for d in _docs() if d["kind"] == "ConfigMap"]
    assert cm["data"]["DELEGATION_JWT_ALLOWED_ISSUERS"] == '["workspace"]'
    assert cm["data"]["DELEGATION_JWT_MAX_LIFETIME_SECONDS"] == "120"


@pytest.mark.parametrize("override,needle", [
    ("delegation.secretRef.name=", "delegation.secretRef"),
    ("delegation.secretRef.key=", "delegation.secretRef"),
    ("controlPlane.audience=workspace.inaiera", "controlPlane.audience"),   # the X-001 mistake
    ("controlPlane.audience=", "controlPlane.audience"),
], ids=["no-name", "no-key", "workspace-audience", "empty-audience"])
def test_the_chart_refuses_an_incomplete_or_cross_domain_contract(override, needle):
    done = _template(override)
    assert done.returncode != 0, "chart rendered a runner that trusts the wrong audience / has no delegation key"
    assert needle in done.stderr


# ---- X-001: the Data Quality service as a delegation issuer ------------------------------------------------------------
def test_the_dq_delegation_key_comes_from_its_own_secret_never_the_configmap():
    docs = _docs()
    (cm,) = [d for d in docs if d["kind"] == "ConfigMap"]
    assert "DQ_DELEGATION_JWT_SECRET" not in cm["data"]
    env = _api_env(docs)["DQ_DELEGATION_JWT_SECRET"]
    assert "value" not in env
    assert env["valueFrom"]["secretKeyRef"] == {"name": "flinksql-dq-delegation-jwt", "key": "secret"}


def test_dq_is_limited_to_the_scopes_in_the_configmap():
    (cm,) = [d for d in _docs() if d["kind"] == "ConfigMap"]
    assert cm["data"]["DQ_DELEGATION_ALLOWED_SCOPES"] == '["flink.jobs.read","flink.jobs.edit"]'


def test_dq_can_be_switched_off_and_then_has_no_key():
    assert "DQ_DELEGATION_JWT_SECRET" not in _api_env(_docs("delegation.dq.enabled=false"))


@pytest.mark.parametrize("overrides,needle", [
    (["delegation.dq.secretRef.name="], "delegation.dq.secretRef.name"),
    (["delegation.dq.secretRef.key="], "delegation.dq.secretRef.key"),
    (["delegation.dq.secretRef.name=flinksql-delegation-jwt"], "must not reuse"),
    (["delegation.dq.secretRef.name=flinksql-internal-token", "delegation.dq.secretRef.key=token"],
     "must not reuse"),
], ids=["no-name", "no-key", "reuses-workspace-key", "reuses-internal-token"])
def test_the_chart_refuses_a_dq_key_that_is_missing_or_shared(overrides, needle):
    done = _template(*overrides)
    assert done.returncode != 0 and needle in done.stderr, done.stderr
