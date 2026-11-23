"""DQ-CFG-001 chart tests — renders the real chart with the helm CLI over a
matrix of integration combinations (CI-style)."""
import itertools
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from app.core.config import Settings

CHART = Path(__file__).resolve().parent.parent / "docker" / "helm" / "dq-service"
HELM = shutil.which("helm")
pytestmark = pytest.mark.skipif(HELM is None, reason="helm CLI not installed")
BASE = ["--set", "tenant.orgId=org-123"]
INTEGRATIONS = {"ucs": "UCS", "dbtRunner": "DBTRUNNER", "flinkRunner": "FLINKSQL"}
EGRESS_APP = {"ucs": "app: catalog", "dbtRunner": "app: dbtrunner",
              "flinkRunner": "app: flinksql-runner"}


def _helm(cmd: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([HELM, cmd, *(["t"] if cmd == "template" else []), str(CHART), *args],
                          capture_output=True, text=True, timeout=60)


def render(*args: str) -> dict[str, dict]:
    p = _helm("template", *BASE, *args)
    assert p.returncode == 0, p.stderr
    docs = [d for d in yaml.safe_load_all(p.stdout) if d]
    return {f"{d['kind']}/{d['metadata']['name']}": d for d in docs}


def _deploy(docs):
    return next(d for k, d in docs.items() if k.startswith("Deployment/"))


def _cm(docs):
    return next(d for k, d in docs.items() if k.startswith("ConfigMap/"))["data"]


def _env(docs):
    c = _deploy(docs)["spec"]["template"]["spec"]["containers"][0]
    return {e["name"]: e for e in c["env"]}


def test_lint_passes():
    p = _helm("lint", *BASE)
    assert p.returncode == 0, p.stdout + p.stderr


@pytest.mark.parametrize("flags", list(itertools.product([True, False], repeat=3)),
                         ids=lambda f: "-".join("on" if x else "off" for x in f))
def test_every_integration_combination_renders_consistently(flags):
    args = []
    for (name, _), on in zip(INTEGRATIONS.items(), flags):
        args += ["--set", f"integrations.{name}.enabled={str(on).lower()}"]
    docs = render(*args)
    env, cm = _env(docs), _cm(docs)
    egress = yaml.safe_dump(next(d for k, d in docs.items()
                                 if k.startswith("NetworkPolicy/"))["spec"]["egress"])
    for (name, prefix), on in zip(INTEGRATIONS.items(), flags):
        secret_env = f"{prefix}_SERVICE_JWT_SECRET"
        url_key = "FLINKSQL_RUNNER_URL" if prefix == "FLINKSQL" else f"{prefix}_URL"
        assert cm[f"{prefix}_ENABLED"] == str(on).lower()
        assert (secret_env in env) is on
        assert (url_key in cm) is on
        assert (EGRESS_APP[name] in egress) is on
        if on:
            ref = env[secret_env]["valueFrom"]["secretKeyRef"]
            assert ref["name"].endswith("-service-jwt")


@pytest.mark.parametrize("name", list(INTEGRATIONS))
@pytest.mark.parametrize("missing,msg", [
    ("url=", "url"), ("serviceJwtSecret.name=", "serviceJwtSecret"),
    ("url=dbt:80", "url"),
])
def test_enabled_integration_with_missing_config_fails_render(name, missing, msg):
    p = _helm("template", *BASE, "--set", f"integrations.{name}.{missing}")
    assert p.returncode != 0 and msg in p.stderr, p.stderr


@pytest.mark.parametrize("name", list(INTEGRATIONS))
def test_disabled_integration_may_omit_config(name):
    render("--set", f"integrations.{name}.enabled=false", "--set", f"integrations.{name}.url=",
           "--set", f"integrations.{name}.serviceJwtSecret.name=")


def test_integrations_must_not_share_a_secret():
    p = _helm("template", *BASE, "--set", "integrations.flinkRunner.serviceJwtSecret.name="
              "dq-ucs-service-jwt")
    assert p.returncode != 0 and "separate signing secrets" in p.stderr


@pytest.mark.parametrize("org", ["REPLACE_ME", ""])
def test_placeholder_org_fails(org):
    p = subprocess.run([HELM, "template", "t", str(CHART), "--set", f"tenant.orgId={org}"],
                       capture_output=True, text=True)
    assert p.returncode != 0


def test_env_and_config_names_match_settings_fields():
    docs = render()
    fields = set(Settings.model_fields)
    names = set(_env(docs)) | set(_cm(docs))
    names -= {"PYTHONDONTWRITEBYTECODE", "HOME", "RUN_MIGRATIONS_ON_START"}
    unknown = {n for n in names if n.lower() not in fields}
    assert not unknown, unknown


def test_container_hardening():
    docs = render()
    spec = _deploy(docs)["spec"]["template"]["spec"]
    c = spec["containers"][0]
    sc, psc = c["securityContext"], spec["securityContext"]
    assert psc["runAsNonRoot"] and psc["seccompProfile"]["type"] == "RuntimeDefault"
    assert sc["readOnlyRootFilesystem"] is True and sc["allowPrivilegeEscalation"] is False
    assert sc["capabilities"]["drop"] == ["ALL"] and sc["seccompProfile"]["type"] == "RuntimeDefault"
    assert spec["automountServiceAccountToken"] is False
    assert {"name": "tmp", "mountPath": "/tmp"} in c["volumeMounts"]
    assert next(v for v in spec["volumes"] if v["name"] == "tmp")["emptyDir"]
    assert c["readinessProbe"]["httpGet"]["path"] == "/readyz" and c["startupProbe"]


@pytest.mark.parametrize("weaken", [
    "securityContext.readOnlyRootFilesystem=false",
    "securityContext.allowPrivilegeEscalation=true",
    "securityContext.runAsNonRoot=false",
    "podSecurityContext.runAsNonRoot=false",
    "securityContext.privileged=true",
    "securityContext.seccompProfile.type=Unconfined",
])
def test_schema_refuses_weakened_hardening(weaken):
    p = _helm("template", *BASE, "--set", weaken)
    assert p.returncode != 0


def test_network_policy_pdb_hpa_and_migration_job():
    docs = render()
    kinds = {k.split("/")[0] for k in docs}
    assert {"NetworkPolicy", "PodDisruptionBudget", "HorizontalPodAutoscaler", "Job",
            "ServiceAccount", "Service", "ConfigMap", "Deployment"} <= kinds
    np = next(d for k, d in docs.items() if k.startswith("NetworkPolicy/"))
    assert set(np["spec"]["policyTypes"]) == {"Ingress", "Egress"}
    pdb = next(d for k, d in docs.items() if k.startswith("PodDisruptionBudget/"))
    assert pdb["spec"]["minAvailable"] == 1
    job = next(d for k, d in docs.items() if k.startswith("Job/"))
    assert job["metadata"]["annotations"]["helm.sh/hook"] == "pre-install,pre-upgrade"
    assert job["spec"]["template"]["spec"]["containers"][0]["securityContext"][
        "readOnlyRootFilesystem"] is True
    assert _cm(docs)["RUN_MIGRATIONS_ON_START"] == "false"
    off = render("--set", "networkPolicy.enabled=false", "--set", "podDisruptionBudget.enabled=false")
    assert not any(k.startswith(("NetworkPolicy/", "PodDisruptionBudget/")) for k in off)


def test_database_password_is_a_secret_not_in_the_url():
    docs = render()
    assert "DATABASE_PASSWORD" in _env(docs)
    assert not re.search(r"://[^@/]*:[^@/]*@", _cm(docs)["DATABASE_URL"])


def test_quality_contract_pinned():
    p = _helm("template", *BASE, "--set", "qualityContract.version=1.0")
    assert p.returncode != 0
