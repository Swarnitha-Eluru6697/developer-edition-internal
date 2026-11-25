"""DBT-010: Run Job pods execute customer dbt code, so their network egress is deny-by-default with NAMED destinations.

Renders the chart (needs `helm` on PATH; skipped otherwise) and checks the Job NetworkPolicy: no `0.0.0.0/0`, no namespaceSelector
that matches every namespace, git only through the egress proxy when one is configured, and a render failure when a destination
is missing or when the public-internet escape hatch is used outside dev.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).resolve().parent.parent / "docker" / "helm" / "dbtrunner"
EGRESS = str(CHART / "ci" / "egress-values.yaml")
pytestmark = pytest.mark.skipif(not shutil.which("helm"), reason="helm not installed")


def _template(*extra: str, egress: bool = True) -> subprocess.CompletedProcess:
    args = ["helm", "template", "r", str(CHART), "--set", "tenant.orgId=acme"] + (["-f", EGRESS] if egress else [])
    return subprocess.run([*args, *extra], capture_output=True, text=True, check=False)


def _docs(*extra: str, egress: bool = True):
    done = _template(*extra, egress=egress)
    assert done.returncode == 0, done.stderr
    return [d for d in yaml.safe_load_all(done.stdout) if d]


def _job_policy(docs) -> dict:
    (pol,) = [d for d in docs if d["kind"] == "NetworkPolicy" and d["metadata"]["name"].endswith("-job")]
    return pol


def _peers(pol) -> list[dict]:
    return [peer for rule in pol["spec"]["egress"] for peer in rule.get("to", [])]


def test_job_egress_has_no_internet_wide_peer_by_default():
    pol = _job_policy(_docs())
    for peer in _peers(pol):
        assert "ipBlock" not in peer or peer["ipBlock"]["cidr"] != "0.0.0.0/0"
    # the only wide selector allowed is cluster DNS (namespaceSelector {} AND the kube-dns pod label)
    for peer in _peers(pol):
        if peer.get("namespaceSelector") == {}:
            assert peer.get("podSelector", {}).get("matchLabels") == {"k8s-app": "kube-dns"}


def test_git_goes_only_to_the_proxy_when_one_is_configured():
    pol = _job_policy(_docs())
    ports = {p["port"] for rule in pol["spec"]["egress"] for p in rule.get("ports", [])}
    assert 3128 in ports and 22 not in ports            # no ssh, no direct 443 to a git host: just the proxy
    proxy_rule = [r for r in pol["spec"]["egress"] if r.get("ports") == [{"protocol": "TCP", "port": 3128}]]
    assert len(proxy_rule) == 1
    assert proxy_rule[0]["to"][0]["podSelector"]["matchLabels"] == {"app": "git-egress"}


def test_the_job_gets_the_proxy_via_the_configmap():
    docs = _docs()
    (cm,) = [d for d in docs if d["kind"] == "ConfigMap"]
    assert cm["data"]["JOB_GIT_PROXY_URL"] == "http://git-egress.platform.svc:3128"


def test_direct_git_hosts_are_exact_when_no_proxy():
    pol = _job_policy(_docs("--set", "networkPolicy.job.git.proxy.url=", "--set", "networkPolicy.job.git.proxy.to=null",
                            "--set", "networkPolicy.job.git.to[0].ipBlock.cidr=140.82.112.3/32"))
    git = [r for r in pol["spec"]["egress"] if r.get("to") == [{"ipBlock": {"cidr": "140.82.112.3/32"}}]]
    assert len(git) == 1 and {p["port"] for p in git[0]["ports"]} == {443}


def test_public_internet_opt_in_in_dev_excludes_private_ranges():
    pol = _job_policy(_docs("--set", "environment=dev", "--set", "networkPolicy.job.allowPublicInternet=true",
                            "--set", "artifactStore.backend=local"))
    wide = [p["ipBlock"] for p in _peers(pol) if "ipBlock" in p and p["ipBlock"]["cidr"] == "0.0.0.0/0"]
    assert len(wide) == 1
    for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16"):
        assert cidr in wide[0]["except"]


@pytest.mark.parametrize("overrides,needle", [
    (["--set", "networkPolicy.job.git.proxy.url=", "--set", "networkPolicy.job.git.proxy.to=null"], "networkPolicy.job.git.to"),
    (["--set", "networkPolicy.job.git.proxy.to=null"], "networkPolicy.job.git.proxy.to is required"),
    (["--set", "networkPolicy.job.warehouse.to=null"], "networkPolicy.job.warehouse.to is required"),
    (["--set", "networkPolicy.job.artifactStore.to=null"], "networkPolicy.job.artifactStore.to is required"),
    (["--set", "networkPolicy.job.allowPublicInternet=true"], "allowPublicInternet is only allowed with environment=dev"),
])
def test_missing_destinations_or_public_internet_outside_dev_refuse_to_render(overrides, needle):
    done = _template(*overrides)
    assert done.returncode != 0 and needle in done.stderr


def test_unconfigured_chart_refuses_to_render():
    done = _template(egress=False)
    assert done.returncode != 0 and "networkPolicy.job.git.to" in done.stderr


# DBT-009: git tokens come from Secrets by name; a malformed reference name is refused.
def test_git_credentials_are_env_from_secrets():
    docs = _docs("--set", "gitCredentials.acme_gh.name=acme-gh", "--set", "gitCredentials.acme_gh.key=token")
    (dep,) = [d for d in docs if d["kind"] == "Deployment"]
    env = {e["name"]: e for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["GIT_CREDENTIAL_ACME_GH"]["valueFrom"]["secretKeyRef"] == {"name": "acme-gh", "key": "token"}
    assert "value" not in env["GIT_CREDENTIAL_ACME_GH"]


def test_a_bad_git_credential_name_is_refused():
    done = _template("--set", "gitCredentials.Bad-Name.name=x", "--set", "gitCredentials.Bad-Name.key=k")
    assert done.returncode != 0 and "credential_ref" in done.stderr
