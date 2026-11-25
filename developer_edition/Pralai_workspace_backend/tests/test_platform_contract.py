"""X-007: cross-repository contract tests — the integration mismatches a single repo's unit tests cannot see.

The highest-impact findings of this review were all mismatches BETWEEN components: a secret the caller mounts that the callee
never reads, a Service URL that does not resolve to the callee's Service, a chart key the application ignores, two services that
disagree on a role or a scope. These tests render every production Helm chart of the Workspace platform and check the rendered
result against the OTHER side of each integration:

  * every chart renders (and the one that cannot be rendered offline says why);
  * every environment variable a chart sets is read by that service (its `Settings` class or a direct `os.environ` read, found by
    reading the source, so no service has to be importable here);
  * every shared signing secret has BOTH ends mounted from a Secret (never a plain value), and no service reuses one Secret for two
    purposes;
  * every service URL Workspace is configured with resolves to a Service the target chart really creates, on the port the URL implies;
  * the roles/capabilities, scopes and DQ permissions agree across services (tests/test_rbac_contract.py,
    tests/test_delegated_tokens.py, tests/test_dq_delegation.py).

Sibling repositories are read from next to this one (or RUNNERS_REPO / DQ_REPO / DAGSTER_REPO); a repository that is not checked out
skips its checks with a reason, so the same file runs in a single-repo CI and in the platform CI. `scripts/platform_gate.sh` runs this
file, every repo's own suite and the live end-to-end scenarios (scripts/e2e) in the order the platform gate needs.
"""
from __future__ import annotations

import ast
import os
import shutil
import subprocess
from functools import cache
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).resolve().parent.parent
ROOT = HERE.parent
RUNNERS = Path(os.environ.get("RUNNERS_REPO", ROOT / "Pralai_workspace_runners"))
DQ = Path(os.environ.get("DQ_REPO", ROOT / "Pralai_dataquality_backend"))
DAGSTER = Path(os.environ.get("DAGSTER_REPO", ROOT / "Pralai-dbt-runner-dagster"))

# service -> (chart dir, app dir, helm --set that make every integration of the chart switched on, release = Service name)
SERVICES = {
    "workspace": (HERE / "docker/helm/workspace", HERE / "app", [], "workspace-acme"),
    "dbtrunner": (RUNNERS / "pralai-dbt-runner/docker/helm/dbtrunner", RUNNERS / "pralai-dbt-runner/app",
                  ["integrations.dataQuality.enabled=true", "integrations.dataQuality.serviceUrl=http://data-quality-acme",
                   # DBT-010: Job pods have no default route anywhere; name the destinations
                   "networkPolicy.job.git.proxy.url=http://git-egress.platform.svc:3128",
                   "networkPolicy.job.git.proxy.to[0].podSelector.matchLabels.app=git-egress",
                   "networkPolicy.job.warehouse.to[0].podSelector.matchLabels.app=trino",
                   "networkPolicy.job.artifactStore.to[0].ipBlock.cidr=10.20.30.40/32"], "dbt-runner-acme"),
    "flinksql-runner": (RUNNERS / "pralai-flinksql-runner/docker/helm/flinksql-runner", RUNNERS / "pralai-flinksql-runner/app", [],
                        "flinksql-runner-acme"),
    "dq": (DQ / "docker/helm/dq-service", DQ / "app", [], "data-quality-acme"),
}
BASE_SET = ["tenant.orgId=acme", "orgId=acme"]

pytestmark = pytest.mark.skipif(not shutil.which("helm"), reason="helm not installed")


def _skip_if_missing(service: str):
    chart = SERVICES[service][0]
    if not (chart / "Chart.yaml").exists():
        pytest.skip(f"{service} repo is not checked out next to this one")


@cache
def _render(service: str) -> tuple[dict, ...]:
    chart, _app, sets, release = SERVICES[service]
    args = ["helm", "template", release, str(chart)]
    for s in (*BASE_SET, *sets):
        args += ["--set", s]
    done = subprocess.run(args, capture_output=True, text=True, timeout=120, check=False)
    assert done.returncode == 0, f"{service} chart does not render: {done.stderr}"
    return tuple(d for d in yaml.safe_load_all(done.stdout) if d)


def _workloads(docs):
    return [d for d in docs if d["kind"] in ("Deployment", "Job", "StatefulSet", "CronJob")]


def _pod_spec(d):
    spec = d["spec"]
    if d["kind"] == "CronJob":
        spec = spec["jobTemplate"]["spec"]
    return spec["template"]["spec"]


def _containers(d):
    return _pod_spec(d).get("containers", [])


# ── the charts render ──────────────────────────────────────────────────────
@pytest.mark.parametrize("service", sorted(SERVICES))
def test_every_production_chart_renders(service):
    _skip_if_missing(service)
    docs = _render(service)
    assert any(d["kind"] == "Deployment" for d in docs) and any(d["kind"] == "Service" for d in docs)


def test_the_dagster_chart_renders_with_its_required_values():
    if not (DAGSTER / "docker/helm/dagster/Chart.yaml").exists():
        pytest.skip("dagster repo is not checked out next to this one")
    done = subprocess.run(["helm", "template", "dg", str(DAGSTER / "docker/helm/dagster"), "--set", "persistence.enabled=false",
                           "--set", "tenant.orgId=acme", "--set", "postgresql.external.host=dagster-db.example"],      # DG-004: a database is required
                          capture_output=True, text=True, timeout=120, check=False)
    assert done.returncode == 0, done.stderr


def test_the_flink_cluster_chart_cannot_be_rendered_offline_and_says_so():
    """It depends on the Flink Kubernetes operator chart, which `helm dependency build` downloads: not available to an offline
    gate. The platform CI must run `helm dependency build` + `helm template` for it (scripts/platform_gate.sh does)."""
    chart = RUNNERS / "pralai-flinksql-runner/docker/helm/flink-cluster"
    if not (chart / "Chart.yaml").exists():
        pytest.skip("runners repo is not checked out next to this one")
    meta = yaml.safe_load((chart / "Chart.yaml").read_text())
    assert [d["name"] for d in meta.get("dependencies", [])] == ["flink-kubernetes-operator"]


# ── the environment a chart sets is the environment the service reads ───────────
def _settings_names(app_dir: Path) -> set[str]:
    """Every env var a service can read: its Settings fields (upper-cased) and every constant passed to os.environ / os.getenv."""
    names: set[str] = set()
    for path in app_dir.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and any(getattr(b, "id", getattr(b, "attr", "")) == "BaseSettings" for b in node.bases):
                names |= {n.target.id.upper() for n in node.body if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)}
            if isinstance(node, ast.Call):
                f = node.func
                is_env = (isinstance(f, ast.Attribute) and f.attr in ("get", "getenv", "setdefault", "pop")
                          and (ast.unparse(f.value) in ("os.environ", "os") if f.attr != "getenv" else ast.unparse(f.value) == "os"))
                if is_env and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    names.add(node.args[0].value)
            if isinstance(node, ast.Subscript) and ast.unparse(node.value) == "os.environ" and isinstance(node.slice, ast.Constant):
                names.add(str(node.slice.value))
    return names


def _rendered_env(docs) -> set[str]:
    cms = {d["metadata"]["name"]: d for d in docs if d["kind"] == "ConfigMap"}
    env: set[str] = set()
    for w in _workloads(docs):
        for c in _containers(w):
            env |= {e["name"] for e in c.get("env", [])}
            for ef in c.get("envFrom", []):
                ref = ef.get("configMapRef", {}).get("name")
                if ref in cms:
                    env |= set(cms[ref].get("data") or {})
    return env


# Variables consumed by something other than the application's own settings code. Each entry says by what.
PLATFORM_ENV = {
    "TRUST_ENVOY_HEADERS", "ENVOY_INTERNAL_SECRET", "WORKSPACE_GIT_ROOT", "WORKER_HEARTBEAT_PATH",   # read directly by Workspace's gateway code
    "PYTHONUNBUFFERED", "PYTHONDONTWRITEBYTECODE", "TZ", "HOME", "PORT",                           # the interpreter / the platform
    "RUN_MIGRATIONS_ON_START",   # read by the DQ image's CMD (docker/Dockerfile), before the application starts
    "TRINO_JWT",                 # the dbt runner hands it to the dbt subprocess, whose profile reads it with env_var()
}


def _k8s_expanded(docs) -> set[str]:
    """Variables used only as `$(NAME)` inside another env value: Kubernetes itself consumes them (DATABASE_URL built from
    DATABASE_PASSWORD, a lease holder id from POD_NAME)."""
    import re
    values = [e["value"] for w in _workloads(docs) for c in _containers(w) for e in c.get("env", []) if isinstance(e.get("value"), str)]
    return {m for v in values for m in re.findall(r"\$\(([A-Z_][A-Z0-9_]*)\)", v)}


@pytest.mark.parametrize("service", sorted(SERVICES))
def test_every_environment_variable_a_chart_sets_is_read_by_its_service(service):
    _skip_if_missing(service)
    known = _settings_names(SERVICES[service][1]) | PLATFORM_ENV
    assert len(known) > 20, "reading the service's settings found too little: the check would be vacuous"
    docs = _render(service)
    unknown = sorted(_rendered_env(docs) - known - _k8s_expanded(docs))
    assert unknown == [], f"{service}: the chart sets variables the service never reads: {unknown}"


# ── shared secrets: both ends mounted from a Secret, none reused for two purposes ────────────────────────────
def _secret_env(service: str) -> dict[str, tuple[str, str]]:
    """env var -> (Secret name, key), for every variable mounted from a Secret by any workload of the chart."""
    out: dict[str, tuple[str, str]] = {}
    for w in _workloads(_render(service)):
        for c in _containers(w):
            for e in c.get("env", []):
                ref = (e.get("valueFrom") or {}).get("secretKeyRef")
                if ref:
                    out[e["name"]] = (ref["name"], ref["key"])
    return out


def _plain_env(service: str) -> set[str]:
    docs = _render(service)
    plain = {e["name"] for w in _workloads(docs) for c in _containers(w) for e in c.get("env", []) if "value" in e}
    for d in docs:
        if d["kind"] == "ConfigMap":
            plain |= set(d.get("data") or {})
    return plain


# (caller service, its env, callee service, the callee's env that must hold the SAME value)
SHARED_SECRETS = [
    ("workspace", "DBTRUNNER_DELEGATION_JWT_SECRET", "dbtrunner", "DELEGATION_JWT_SECRET"),
    ("workspace", "FLINKSQL_DELEGATION_JWT_SECRET", "flinksql-runner", "DELEGATION_JWT_SECRET"),
    ("workspace", "DQ_DELEGATION_JWT_SECRET", "dq", "DELEGATION_JWT_SECRET"),
    ("workspace", "DQ_SERVICE_JWT_SECRET", "dq", "INBOUND_SERVICE_JWT_SECRET"),
    ("dq", "DBTRUNNER_DELEGATION_JWT_SECRET", "dbtrunner", "DQ_DELEGATION_JWT_SECRET"),
    ("dq", "FLINKSQL_DELEGATION_JWT_SECRET", "flinksql-runner", "DQ_DELEGATION_JWT_SECRET"),
    ("dq", "DBTRUNNER_SERVICE_JWT_SECRET", "dbtrunner", "SERVICE_JWT_SECRET"),
    ("dq", "FLINKSQL_SERVICE_JWT_SECRET", "flinksql-runner", "INTERNAL_SERVICE_TOKEN"),
    ("workspace", "FLINKSQL_INTERNAL_TOKEN", "flinksql-runner", "INTERNAL_SERVICE_TOKEN"),
    ("workspace", "DBTRUNNER_INTERNAL_TOKEN", "dbtrunner", "INTERNAL_SERVICE_TOKEN"),
]


@pytest.mark.parametrize("caller,caller_env,callee,callee_env", SHARED_SECRETS,
                         ids=[f"{a}.{b}->{c}.{d}" for a, b, c, d in SHARED_SECRETS])
def test_a_shared_secret_is_mounted_from_a_secret_at_both_ends(caller, caller_env, callee, callee_env):
    _skip_if_missing(caller)
    _skip_if_missing(callee)
    mine, theirs = _secret_env(caller), _secret_env(callee)
    assert caller_env in mine, f"{caller} does not mount {caller_env} from a Secret: {callee} cannot verify / be called with it"
    assert callee_env in theirs, (f"{callee} does not mount {callee_env} from a Secret, but {caller}'s {caller_env} is meant to be "
                                  f"verified by it: every call would be refused")
    assert caller_env not in _plain_env(caller) and callee_env not in _plain_env(callee), "a signing secret is a plain value"


@pytest.mark.parametrize("service", sorted(SERVICES))
def test_no_service_reuses_one_secret_for_two_purposes(service):
    _skip_if_missing(service)
    signing = {env: ref for env, ref in _secret_env(service).items()
               if any(w in env for w in ("JWT_SECRET", "INTERNAL_TOKEN", "SERVICE_TOKEN"))}
    seen: dict[tuple[str, str], str] = {}
    for env, ref in signing.items():
        assert ref not in seen, f"{service}: {env} and {seen[ref]} are mounted from the same Secret {ref}"
        seen[ref] = env


@pytest.mark.parametrize("service", sorted(SERVICES))
def test_signing_secrets_are_never_plain_values_in_any_chart(service):
    _skip_if_missing(service)
    plain = [n for n in _plain_env(service) if any(w in n for w in ("JWT_SECRET", "PASSWORD", "_TOKEN", "SECRET_KEY", "API_KEY"))
             and not n.endswith(("_SECRET_NAME", "_SECRET_KEY", "_URL", "_TOKEN_SECRET_NAME", "_TOKEN_SECRET_KEY", "_ENABLED"))]
    assert plain == [], f"{service}: credentials appear as plain values: {plain}"


# ── service URLs resolve to Services that exist ─────────────────────────────────────────────────────────────────
WORKSPACE_URLS = [("DBTRUNNER_URL", "dbtrunner"), ("FLINKSQL_RUNNER_URL", "flinksql-runner"), ("DQ_SERVICE_URL", "dq")]


@pytest.mark.parametrize("env,callee", WORKSPACE_URLS)
def test_workspaces_service_urls_resolve_to_the_callees_service(env, callee):
    _skip_if_missing("workspace")
    _skip_if_missing(callee)
    cm = next(d for d in _render("workspace") if d["kind"] == "ConfigMap" and d["metadata"]["name"].endswith("-config"))
    url = cm["data"][env]
    from urllib.parse import urlparse
    u = urlparse(url)
    service_name = (u.hostname or "").split(".")[0]
    port = u.port or 80
    services = {d["metadata"]["name"]: d for d in _render(callee) if d["kind"] == "Service"}
    assert service_name in services, (f"Workspace's {env}={url} points at Service '{service_name}', but the {callee} chart creates "
                                      f"{sorted(services)}")
    assert port in [p["port"] for p in services[service_name]["spec"]["ports"]], f"{env}: {callee}'s Service has no port {port}"


def test_the_callees_service_selects_the_pods_its_deployment_creates():
    for service in SERVICES:
        if not (SERVICES[service][0] / "Chart.yaml").exists():
            continue
        docs = _render(service)
        svc = next(d for d in docs if d["kind"] == "Service")
        api = [d for d in docs if d["kind"] == "Deployment"
               and all(d["spec"]["template"]["metadata"]["labels"].get(k) == v for k, v in svc["spec"]["selector"].items())]
        assert api, f"{service}: the Service selects no Deployment's pods"


# ── shared vocabularies that two charts must agree on ─────────────────────────────────────────────────────────────
def _config_values(service: str, keys: tuple[str, ...]) -> dict[str, str]:
    out: dict[str, str] = {}
    for d in _render(service):
        if d["kind"] == "ConfigMap":
            out.update({k: v for k, v in (d.get("data") or {}).items() if k in keys})
    return out


def test_dq_and_the_flink_runner_share_one_quality_contract_and_topic_set():
    """DQ writes quality results/gates/quarantine to these topics and the Flink runner consumes them: a different version or topic
    name on either side silently drops every quality event."""
    _skip_if_missing("dq")
    _skip_if_missing("flinksql-runner")
    keys = ("QUALITY_CONTRACT_VERSION", "QUALITY_RESULTS_TOPIC", "QUALITY_GATE_TOPIC", "QUALITY_QUARANTINE_TOPIC")
    dq, flink = _config_values("dq", keys), _config_values("flinksql-runner", keys)
    assert set(dq) == set(flink) == set(keys)
    assert dq == flink


def test_every_service_is_told_the_same_org_and_runs_with_the_same_environment_name():
    org = {}
    for service in SERVICES:
        if not (SERVICES[service][0] / "Chart.yaml").exists():
            continue
        cfg = _config_values(service, ("ORG_ID", "ENVIRONMENT"))
        assert cfg.get("ENVIRONMENT") in ("prod", "staging"), f"{service}: a production chart must not run as dev ({cfg})"
        org[service] = cfg.get("ORG_ID")
    assert len(set(org.values())) == 1, f"services of one data plane disagree on the org: {org}"


def test_the_delegation_audiences_the_charts_accept_are_the_ones_workspace_signs_for():
    """Each callee's accepted audience (chart value) is the audience Workspace mints tokens for (service_tokens constants)."""
    from app.core import service_tokens as st

    expected = {"dq": st.DQ_AUDIENCE, "dbtrunner": st.DBTRUNNER_AUDIENCE, "flinksql-runner": st.FLINK_AUDIENCE}
    for service, audience in expected.items():
        if not (SERVICES[service][0] / "Chart.yaml").exists():
            continue
        cfg = _config_values(service, ("CONTROL_PLANE_AUDIENCE", "DELEGATION_JWT_AUDIENCE", "SERVICE_AUDIENCE"))
        if "CONTROL_PLANE_AUDIENCE" in cfg:
            assert cfg["CONTROL_PLANE_AUDIENCE"] == audience, f"{service} accepts '{cfg['CONTROL_PLANE_AUDIENCE']}', Workspace signs for '{audience}'"
