"""SQL jobs on a Workspace connection and Python jobs: the dbt Runner chart wires their opt-in settings.

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

WORKSPACE = ["--set", "jobs.workspace.enabled=true", "--set", "jobs.workspace.url=http://workspace.ws.svc:8080"]


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


def _config(docs) -> dict:
    (cm,) = [d for d in docs if d["kind"] == "ConfigMap"]
    return cm["data"]


def test_nothing_is_wired_by_default():
    docs = _docs()
    assert "WORKSPACE_URL" not in _config(docs) and "PIP_INDEX_URL" not in _config(docs)
    assert "WORKSPACE_JOB_JWT_SECRET" not in _api_env(docs)


def test_the_workspace_signing_key_comes_from_its_own_secret_never_the_configmap():
    docs = _docs(*WORKSPACE)
    assert _config(docs)["WORKSPACE_URL"] == "http://workspace.ws.svc:8080"
    assert "WORKSPACE_JOB_JWT_SECRET" not in _config(docs)
    env = _api_env(docs)
    assert env["WORKSPACE_JOB_JWT_SECRET"]["valueFrom"]["secretKeyRef"] == {"name": "dbtrunner-workspace-job-jwt", "key": "secret"}
    refs = {n: (env[n]["valueFrom"]["secretKeyRef"]["name"], env[n]["valueFrom"]["secretKeyRef"]["key"])
            for n in ("WORKSPACE_JOB_JWT_SECRET", "SCHEDULER_JWT_SECRET", "INTERNAL_SERVICE_TOKEN", "DELEGATION_JWT_SECRET")}
    assert len(set(refs.values())) == 4, refs


def test_the_workspace_url_is_required_when_enabled():
    done = _template("--set", "jobs.workspace.enabled=true")
    assert done.returncode != 0 and "jobs.workspace.url" in done.stderr


@pytest.mark.parametrize("overrides", [
    ["--set", "jobs.workspace.secretRef.name=dbtrunner-scheduler-jwt", "--set", "jobs.workspace.secretRef.key=secret"],
    ["--set", "jobs.workspace.secretRef.name=dbtrunner-delegation-jwt", "--set", "jobs.workspace.secretRef.key=secret"],
])
def test_the_workspace_key_must_not_reuse_another_identitys_secret(overrides):
    done = _template(*WORKSPACE, *overrides)
    assert done.returncode != 0 and "must not reuse the Secret" in done.stderr


def test_the_pip_index_is_rendered():
    assert _config(_docs("--set", "jobs.python.pipIndexUrl=https://pypi.internal/simple"))["PIP_INDEX_URL"] == "https://pypi.internal/simple"


def _job_egress(docs):
    (pol,) = [d for d in docs if d["kind"] == "NetworkPolicy" and d["metadata"]["name"].endswith("-job")]
    return pol["spec"]["egress"]


def _ports(rule):
    return [p["port"] for p in rule["ports"]]


def test_job_pods_reach_the_workspace_only_through_a_peer_you_named():
    peer = "networkPolicy.job.workspace.to[0].ipBlock.cidr=10.9.9.9/32"
    rules = _job_egress(_docs(*WORKSPACE, "--set", peer))
    assert any(r.get("to") == [{"ipBlock": {"cidr": "10.9.9.9/32"}}] and _ports(r) == [8080] for r in rules)
    assert not any(_ports(r) == [8080] for r in _job_egress(_docs(*WORKSPACE)))      # no peer named, no rule
    disabled = _job_egress(_docs("--set", peer))
    assert not any(r.get("to") == [{"ipBlock": {"cidr": "10.9.9.9/32"}}] for r in disabled)


def test_job_pods_reach_the_package_index_only_when_one_is_configured():
    peer = "networkPolicy.job.packageIndex.to[0].ipBlock.cidr=10.8.8.8/32"
    rules = _job_egress(_docs("--set", "jobs.python.pipIndexUrl=https://pypi.internal/simple", "--set", peer))
    assert any(r.get("to") == [{"ipBlock": {"cidr": "10.8.8.8/32"}}] and _ports(r) == [443] for r in rules)
    none = _job_egress(_docs("--set", peer))
    assert not any(r.get("to") == [{"ipBlock": {"cidr": "10.8.8.8/32"}}] for r in none)
