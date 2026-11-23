"""DG-001: the production Dagster chart renders, and deploys no scheduling of its own.

Needs `helm` on PATH (skipped otherwise); imports nothing from the package, so it runs anywhere.
"""
from __future__ import annotations

import shutil
import subprocess
from collections import Counter
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).resolve().parents[2] / "docker" / "helm" / "dagster"
pytestmark = pytest.mark.skipif(not shutil.which("helm"), reason="helm not installed")
# DG-019: the compute-log volume is shared by two pods, so the chart needs a ReadWriteMany class and will not guess one.
RWX_CLASS = "persistence.computeLogs.storageClass=nfs-rwx"


@pytest.fixture(scope="module")
def docs():
    done = subprocess.run(["helm", "template", "d", str(CHART), "--set", "postgresql.external.host=db.example", "--set", "tenant.orgId=acme",
                           "--set", RWX_CLASS], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr          # a chart that does not render cannot be deployed
    return [d for d in yaml.safe_load_all(done.stdout) if d]


def _deployments(docs):
    return [d for d in docs if d["kind"] == "Deployment"]


def test_chart_deploys_code_server_daemon_and_webserver(docs):
    names = sorted(d["metadata"]["labels"]["app.kubernetes.io/component"] for d in _deployments(docs))
    assert names == ["code-server", "daemon", "webserver"]


@pytest.mark.parametrize("env_name", ["DAGSTER_PG_HOST", "DAGSTER_PG_PASSWORD", "DAGSTER_CODE_SERVER_HOST", "DAGSTER_HOME"])
def test_every_pod_gets_its_database_and_code_server_env(docs, env_name):
    for d in _deployments(docs):
        env = [e["name"] for e in d["spec"]["template"]["spec"]["containers"][0]["env"]]
        assert Counter(env)[env_name] == 1, (d["metadata"]["name"], env_name)


RUNNER_ENV = ["DBT_RUNNER_BASE_URL", "SCHEDULER_JWT_SECRET", "ORG_ID"]


def _component_env(docs):
    return {d["metadata"]["labels"]["app.kubernetes.io/component"]: [e["name"] for e in d["spec"]["template"]["spec"]["containers"][0]["env"]]
            for d in _deployments(docs)}


@pytest.mark.parametrize("env_name", RUNNER_ENV)
def test_dg003_only_the_code_server_holds_the_dbt_runner_identity(docs, env_name):
    """The signing key for Dagster's scoped runner tokens (project enumeration, run triggering) is mounted into the one
    component that evaluates sensors and runs jobs. A compromised webserver or daemon does not yield it."""
    env = _component_env(docs)
    assert Counter(env["code-server"])[env_name] == 1
    assert env_name not in env["webserver"] and env_name not in env["daemon"]


def test_the_runner_identity_is_nowhere_but_the_code_server_and_the_ephemeral_helm_test(docs):
    holders = []
    for d in docs:
        pod = d["spec"]["template"]["spec"] if d["kind"] == "Deployment" else d["spec"] if d["kind"] == "Pod" else None
        if pod and any(e["name"] == "SCHEDULER_JWT_SECRET" for c in pod["containers"] for e in c.get("env", [])):
            holders.append(d["metadata"]["labels"]["app.kubernetes.io/component"])
    assert sorted(holders) == ["code-server", "test"]       # the helm-test pod exists only during `helm test`


def test_the_code_server_serves_the_generic_location_not_the_per_repo_manager(docs):
    """This chart has no sensors: the runner's own scheduler must therefore stay on (see DEPLOYMENT.md)."""
    (code,) = [d for d in _deployments(docs) if d["metadata"]["labels"]["app.kubernetes.io/component"] == "code-server"]
    args = code["spec"]["template"]["spec"]["containers"][0]["args"]
    assert "inaiera_dbt_runner_dags.definitions" in args
    assert "inaiera_dbt_runner_dags.location_manager" not in args


# ---- DG-002: the Dagster UI/GraphQL is an admin surface with no authentication of its own ------------
# Internal-only by default. An ingress is refused unless an auth layer is declared, and the webserver
# pod only accepts traffic from that layer's namespace, so it can't be reached around it.
def _render(*extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(["helm", "template", "d", str(CHART), "--set", "postgresql.external.host=db.example", "--set", "tenant.orgId=acme",
                           "--set", RWX_CLASS, *extra], capture_output=True, text=True, check=False)


def _kinds(done, kind):
    assert done.returncode == 0, done.stderr
    return [d for d in yaml.safe_load_all(done.stdout) if d and d["kind"] == kind]


GATEWAY_SETS = {
    "webserver.ingress.enabled": "true",
    "webserver.ingress.className": "envoy",
    "webserver.ingress.host": "dagster.acme.example",
    "webserver.ingress.tls[0].secretName": "dagster-tls",
    "webserver.ingress.tls[0].hosts[0]": "dagster.acme.example",
    "webserver.ingress.auth.mode": "gateway",
    "webserver.ingress.auth.fromNamespace": "inaiera-gateway",
}
AUTH_URL = r"webserver.ingress.annotations.nginx\.ingress\.kubernetes\.io/auth-url"
AUTH_SIGNIN = r"webserver.ingress.annotations.nginx\.ingress\.kubernetes\.io/auth-signin"


def _sets(base: dict, **changes) -> list[str]:
    """helm `--set` args for `base` with changes applied; a value of None drops the key.
    Keys in `changes` are written with "|" for ".", since kwargs can't contain dots."""
    merged = {**base, **{k.replace("|", "."): v for k, v in changes.items()}}
    return [a for k, v in merged.items() if v is not None for a in ("--set", f"{k}={v}")]


def test_default_install_is_internal_only(docs):
    assert not [d for d in docs if d["kind"] == "Ingress"]
    (svc,) = [d for d in docs if d["kind"] == "Service" and d["metadata"]["labels"]["app.kubernetes.io/component"] == "webserver"]
    assert svc["spec"]["type"] == "ClusterIP"


def _parsed(done):
    assert done.returncode == 0, done.stderr
    return [d for d in yaml.safe_load_all(done.stdout) if d]


def _policy(docs, component):
    if isinstance(docs, subprocess.CompletedProcess):
        docs = _parsed(docs)
    (np,) = [d for d in docs if d["kind"] == "NetworkPolicy" and d["spec"]["podSelector"]["matchLabels"].get("app.kubernetes.io/component") == component]
    return np


def test_default_webserver_accepts_no_inbound_traffic_at_all(docs):
    np = _policy(docs, "webserver")
    assert np["spec"]["policyTypes"] == ["Ingress", "Egress"]
    assert np["spec"].get("ingress") in (None, [])        # default deny; operators use kubectl port-forward


@pytest.mark.parametrize("extra,needle", [
    (["--set", "webserver.ingress.enabled=true"], "auth"),                                    # no auth layer declared at all
    (_sets(GATEWAY_SETS, **{"webserver|ingress|auth|mode": "none"}), "auth.mode"),             # not a recognised mode
    (_sets(GATEWAY_SETS, **{"webserver|ingress|auth|fromNamespace": ""}), "fromNamespace"),    # nothing to restrict the pod to
    (_sets(GATEWAY_SETS, **{"webserver|ingress|className": ""}), "className"),                 # would land on the default controller
    (_sets(GATEWAY_SETS, **{"webserver|ingress|host": "dagster.example.com"}), "host"),        # the placeholder host
    (_sets({k: v for k, v in GATEWAY_SETS.items() if ".tls[" not in k}), "tls"),               # plain HTTP
    (_sets(GATEWAY_SETS, **{"networkPolicy|enabled": "false"}), "networkPolicy"),              # the bypass guard cannot be removed
    (["--set", "webserver.service.type=LoadBalancer"], "type"),                                # exposes the pod without any auth
    (["--set", "webserver.service.type=NodePort"], "type"),
], ids=["no-auth", "bad-mode", "no-namespace", "no-class", "placeholder-host", "no-tls", "no-netpol", "loadbalancer", "nodeport"])
def test_chart_refuses_an_unauthenticated_or_unrestricted_exposure(extra, needle):
    done = _render(*extra)
    assert done.returncode != 0, "chart rendered an exposed Dagster UI without an enforced auth layer"
    assert needle in done.stderr


def test_gateway_mode_exposes_the_ui_only_to_the_gateway_namespace():
    done = _render(*_sets(GATEWAY_SETS))
    (ing,) = _kinds(done, "Ingress")
    assert ing["spec"]["ingressClassName"] == "envoy" and ing["spec"]["tls"]
    np = _policy(done, "webserver")
    (rule,) = np["spec"]["ingress"]
    assert rule["from"] == [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "inaiera-gateway"}}}]
    assert [p["port"] for p in rule["ports"]] == [3000]


def test_oidc_proxy_mode_needs_both_external_auth_annotations():
    base = {**GATEWAY_SETS, "webserver.ingress.auth.mode": "oidc-proxy"}
    assert _render(*_sets(base)).returncode != 0                                       # neither annotation
    assert _render(*_sets(base, **{AUTH_URL.replace(".", "|"): None}), "--set", f"{AUTH_URL}=https://sso.example/oauth2/auth").returncode != 0  # auth-signin missing
    done = _render(*_sets({**base, AUTH_URL: "https://sso.example/oauth2/auth", AUTH_SIGNIN: "https://sso.example/oauth2/start"}))
    (ing,) = _kinds(done, "Ingress")
    assert ing["metadata"]["annotations"]["nginx.ingress.kubernetes.io/auth-signin"] == "https://sso.example/oauth2/start"


# ---- DG-005: Dagster reaches the dbt Runner at the Service the runner chart really creates ------------
# The runner chart names its Service dbt-runner-<orgId> (lower-cased, "_" -> "-", at most 47 chars) and
# exposes port 80 (targetPort 8080). The old default, http://dbtrunner-svc:8082, matched neither.
import os
from urllib.parse import urlparse

RUNNER_CHART = Path(os.environ.get("DBTRUNNER_CHART", CHART.parents[3] / "InaiEra_workspace_runners"
                                   / "inaiera-dbt-runner" / "docker" / "helm" / "dbtrunner"))


def _runner_url(*extra: str, namespace: str = "dp-prod") -> str:
    done = _render("--namespace", namespace, *extra)
    assert done.returncode == 0, done.stderr
    urls = {e["value"] for d in yaml.safe_load_all(done.stdout) if d and d["kind"] == "Deployment"
            for e in d["spec"]["template"]["spec"]["containers"][0]["env"] if e["name"] == "DBT_RUNNER_BASE_URL"}
    assert len(urls) == 1, urls
    return urls.pop()


def test_runner_url_is_computed_from_the_tenant_and_namespace():
    assert _runner_url() == "http://dbt-runner-acme.dp-prod.svc.cluster.local"


def test_default_url_is_no_longer_the_unrelated_dbtrunner_svc_on_8082():
    url = _runner_url()
    assert "dbtrunner-svc" not in url and ":8082" not in url


@pytest.mark.parametrize("org,svc", [
    ("ACME_Corp", "dbt-runner-acme-corp"),                       # lower-cased, "_" -> "-" (same as the runner chart)
    ("11111111-2222-3333-4444-555555555555", "dbt-runner-11111111-2222-3333-4444-555555555555"[:47]),   # real org UUID, trimmed to 47
])
def test_runner_service_name_follows_the_runner_charts_naming_rule(org, svc):
    url = _runner_url("--set", f"tenant.orgId={org}")
    assert urlparse(url).hostname == f"{svc.rstrip('-')}.dp-prod.svc.cluster.local"


def test_runner_in_another_namespace_and_on_another_port():
    url = _runner_url("--set", "dbtRunner.namespace=dp-shared", "--set", "dbtRunner.servicePort=8080")
    assert url == "http://dbt-runner-acme.dp-shared.svc.cluster.local:8080"


def test_an_explicit_base_url_still_wins():
    assert _runner_url("--set", "dbtRunner.baseUrl=http://custom.example:9000/") == "http://custom.example:9000"


def test_without_a_tenant_or_explicit_url_the_chart_refuses_to_guess():
    done = subprocess.run(["helm", "template", "d", str(CHART), "--set", "postgresql.external.host=db.example"],
                          capture_output=True, text=True, check=False)
    assert done.returncode != 0
    assert "tenant.orgId" in done.stderr


@pytest.mark.skipif(not (RUNNER_CHART / "Chart.yaml").exists(), reason="runner chart not checked out next to this repo (set DBTRUNNER_CHART)")
@pytest.mark.parametrize("org", ["acme", "ACME_Corp", "11111111-2222-3333-4444-555555555555"])
def test_computed_url_resolves_to_the_service_the_runner_chart_actually_renders(org):
    """The contract test: render BOTH charts and compare what Dagster calls with what the runner exposes."""
    runner = subprocess.run(["helm", "template", "r", str(RUNNER_CHART), "--namespace", "dp-prod", "--set", f"tenant.orgId={org}",
                             "-f", str(RUNNER_CHART / "ci" / "egress-values.yaml")],      # DBT-010: the runner chart needs its Job egress named
                            capture_output=True, text=True, check=False)
    assert runner.returncode == 0, runner.stderr
    (svc,) = [d for d in yaml.safe_load_all(runner.stdout) if d and d["kind"] == "Service"]
    url = urlparse(_runner_url("--set", f"tenant.orgId={org}"))
    assert url.hostname == f"{svc['metadata']['name']}.dp-prod.svc.cluster.local"
    assert (url.port or 80) == svc["spec"]["ports"][0]["port"]


def test_chart_ships_a_helm_test_that_proves_connectivity_to_the_runner():
    done = _render()
    (pod,) = [d for d in yaml.safe_load_all(done.stdout) if d and d["kind"] == "Pod"]
    assert pod["metadata"]["annotations"]["helm.sh/hook"] == "test"
    c = pod["spec"]["containers"][0]
    env = {e["name"]: e for e in c["env"]}
    assert env["DBT_RUNNER_BASE_URL"]["value"] == "http://dbt-runner-acme.default.svc.cluster.local"
    assert env["SCHEDULER_JWT_SECRET"]["valueFrom"]["secretKeyRef"]["name"] == "dbtrunner-scheduler-jwt"
    assert env["ORG_ID"]["value"] == "acme"
    script = " ".join(c["command"] + c.get("args", []))
    # reachability AND the token: the same internal route the schedule sensor calls
    assert "/healthz" in script and "/api/v1/internal/projects/scheduled" in script
    assert "X-Internal-Service-Token" in script
    # DBT-003: it proves the SAME scoped identity Dagster really uses, not the static token
    assert "dbt.projects.read" in script and "SCHEDULER_JWT_SECRET" in script
    assert "INTERNAL_SERVICE_TOKEN" not in script


@pytest.mark.parametrize("org", ["<org id>", "acme corp", "acme.prod", "-acme"])
def test_an_org_id_that_cannot_name_a_service_is_refused(org):
    done = _render("--set", f"tenant.orgId={org}")
    assert done.returncode != 0, f"rendered a runner URL for the impossible org id {org!r}"
    assert "tenant.orgId" in done.stderr


# ---- DBT-003: Dagster holds its own scoped scheduler key, not the runner's static internal token ----------
def test_no_dagster_pod_carries_the_static_internal_token(docs):
    for d in docs:
        if d["kind"] in ("Deployment", "Pod"):
            env = [e["name"] for e in d["spec"]["template"]["spec"]["containers"][0]["env"]] if d["kind"] == "Deployment" \
                else [e["name"] for e in d["spec"]["containers"][0]["env"]]
            assert "INTERNAL_SERVICE_TOKEN" not in env, d["metadata"]["name"]


def test_the_scheduler_secret_is_configurable_and_never_a_plain_value():
    done = _render("--set", "dbtRunner.schedulerJwtSecretRef.name=acme-sched", "--set", "dbtRunner.schedulerJwtSecretRef.key=k")
    (code,) = [d for d in _kinds(done, "Deployment") if d["metadata"]["labels"]["app.kubernetes.io/component"] == "code-server"]
    env = {e["name"]: e for e in code["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert "value" not in env["SCHEDULER_JWT_SECRET"]
    assert env["SCHEDULER_JWT_SECRET"]["valueFrom"]["secretKeyRef"] == {"name": "acme-sched", "key": "k"}


@pytest.mark.parametrize("override", ["dbtRunner.schedulerJwtSecretRef.name=", "dbtRunner.schedulerJwtSecretRef.key="], ids=["no-name", "no-key"])
def test_the_chart_refuses_a_missing_scheduler_secret(override):
    done = _render("--set", override)
    assert done.returncode != 0 and "schedulerJwtSecretRef" in done.stderr


def test_the_chart_refuses_the_runners_internal_token_secret_as_the_scheduler_key():
    done = _render("--set", "dbtRunner.schedulerJwtSecretRef.name=dbtrunner-internal-token")
    assert done.returncode != 0
    assert "internal" in done.stderr.lower()



# ---- DG-020: every Dagster pod is default-deny; each component gets only what it needs ---------------------------
def _peers(rule):
    return rule.get("to") or rule.get("from") or []


def _ports(rule):
    return sorted((p["protocol"], p["port"]) for p in rule.get("ports", []))


def _egress_targets(np):
    """{'dns','postgres','code','runner','git'} for each egress rule of a policy."""
    out = {}
    for r in np["spec"].get("egress", []):
        peers = _peers(r)
        if any("podSelector" in p and p["podSelector"]["matchLabels"].get("k8s-app") == "kube-dns" for p in peers):
            out["dns"] = r
        elif any("ipBlock" in p for p in peers):
            out["git"] = r
        elif any(p.get("podSelector", {}).get("matchLabels", {}).get("app.kubernetes.io/component") == "code-server" for p in peers):
            out["code"] = r
        elif _ports(r) == [("TCP", 5432)]:
            out["postgres"] = r
        else:
            out["runner"] = r
    return out


def test_there_is_a_default_deny_for_every_dagster_pod_in_both_directions(docs):
    (deny,) = [d for d in docs if d["kind"] == "NetworkPolicy" and d["metadata"]["name"].endswith("-default-deny")]
    assert deny["spec"]["policyTypes"] == ["Ingress", "Egress"]
    assert not deny["spec"].get("ingress") and not deny["spec"].get("egress")
    sel = deny["spec"]["podSelector"]["matchLabels"]
    assert "app.kubernetes.io/component" not in sel                     # all components, not just one
    for d in _deployments(docs):
        labels = d["spec"]["template"]["metadata"]["labels"]
        assert all(labels.get(k) == v for k, v in sel.items()), d["metadata"]["name"]


def test_every_policy_selects_exactly_the_pods_the_chart_deploys(docs):
    """A selector that matches nothing is a silent hole (or an outage): each component's policy must hit its own pods."""
    labels = {d["metadata"]["labels"]["app.kubernetes.io/component"]: d["spec"]["template"]["metadata"]["labels"] for d in _deployments(docs)}
    for component in ("webserver", "daemon", "code-server"):
        sel = _policy(docs, component)["spec"]["podSelector"]["matchLabels"]
        assert all(labels[component].get(k) == v for k, v in sel.items()), component
    code = _policy(docs, "code-server")
    for peer in [p for r in code["spec"]["ingress"] for p in r["from"]]:
        comp = peer["podSelector"]["matchLabels"]["app.kubernetes.io/component"]
        assert all(labels[comp].get(k) == v for k, v in peer["podSelector"]["matchLabels"].items())
    target = _egress_targets(_policy(docs, "webserver"))["code"]["to"][0]["podSelector"]["matchLabels"]
    assert all(labels["code-server"].get(k) == v for k, v in target.items())


def test_the_code_server_takes_grpc_from_the_webserver_and_the_daemon_only(docs):
    (rule,) = _policy(docs, "code-server")["spec"]["ingress"]
    assert sorted(p["podSelector"]["matchLabels"]["app.kubernetes.io/component"] for p in rule["from"]) == ["daemon", "webserver"]
    assert _ports(rule) == [("TCP", 4000)]
    assert all("namespaceSelector" not in p and "ipBlock" not in p for p in rule["from"])      # nothing from outside the release


def test_the_daemon_takes_no_inbound_traffic(docs):
    assert _policy(docs, "daemon")["spec"].get("ingress") in (None, [])


@pytest.mark.parametrize("component", ["webserver", "daemon"])
def test_the_webserver_and_the_daemon_may_reach_only_dns_postgres_and_the_code_server(docs, component):
    targets = _egress_targets(_policy(docs, component))
    assert sorted(targets) == ["code", "dns", "postgres"]                # no dbt Runner, no Git, no internet
    assert _ports(targets["code"]) == [("TCP", 4000)]


def test_the_code_server_may_reach_dns_postgres_and_the_runner_only(docs):
    """DG-009: no Git rule any more — the git sensor asks the dbt Runner for each project's commit."""
    targets = _egress_targets(_policy(docs, "code-server"))
    assert sorted(targets) == ["dns", "postgres", "runner"]
    assert _ports(targets["runner"]) == [("TCP", 8080)]
    assert targets["runner"]["to"] == [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "default"}}}]


def test_no_dagster_pod_can_reach_the_internet(docs):
    for np in (d for d in docs if d["kind"] == "NetworkPolicy"):
        for rule in np["spec"].get("egress", []):
            assert not any("ipBlock" in p for p in _peers(rule)), np["metadata"]["name"]


# ── DG-009: the advertised Git credential / Git egress settings are gone, and an old values file is refused loudly ──────────────────
@pytest.mark.parametrize("setting", ["codeServer.gitCredentials.enabled=true", "codeServer.gitCredentials.secretRef=x",
                                     "networkPolicy.git.enabled=true", "networkPolicy.git.cidr=10.0.0.0/8"])
def test_the_removed_git_settings_are_refused_not_silently_ignored(setting):
    done = _render("--set", setting)
    assert done.returncode != 0 and ("gitCredentials" in done.stderr or "networkPolicy.git" in done.stderr)


def test_the_chart_mounts_no_git_credential_anywhere(docs):
    for d in _deployments(docs):
        pod = d["spec"]["template"]["spec"]
        names = [e["name"] for e in pod["containers"][0]["env"]] + [v["name"] for v in pod.get("volumes", [])]
        assert not [n for n in names if "git" in n.lower()], (d["metadata"]["name"], names)


def test_the_runner_rule_follows_the_runners_namespace_and_can_be_overridden():
    done = _render("--set", "dbtRunner.namespace=data-plane")
    r = _egress_targets(_policy(done, "code-server"))["runner"]
    assert r["to"] == [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "data-plane"}}}]
    over = _render("--set", "networkPolicy.dbtRunner.to[0].ipBlock.cidr=10.9.0.0/16", "--set", "networkPolicy.dbtRunner.ports[0]=9000")
    (r,) = [r for r in _policy(over, "code-server")["spec"]["egress"] if _ports(r) == [("TCP", 9000)]]   # selected by port: it is an ipBlock now
    assert r["to"] == [{"ipBlock": {"cidr": "10.9.0.0/16"}}]


def test_postgres_egress_is_configurable():
    done = _render("--set", "networkPolicy.postgres.to[0].ipBlock.cidr=10.1.2.3/32", "--set", "networkPolicy.postgres.ports[0]=6432")
    for component in ("webserver", "daemon", "code-server"):
        pg = [r for r in _policy(done, component)["spec"]["egress"] if _peers(r) == [{"ipBlock": {"cidr": "10.1.2.3/32"}}]]
        assert len(pg) == 1 and _ports(pg[0]) == [("TCP", 6432)], component


def test_turning_the_policies_off_renders_none_of_them():
    done = _render("--set", "networkPolicy.enabled=false")
    assert done.returncode == 0, done.stderr
    assert not [d for d in _parsed(done) if d["kind"] == "NetworkPolicy"]


def test_the_helm_test_pod_is_under_default_deny_and_uses_the_code_servers_runner_rule(docs):
    """Otherwise `helm test` could pass while the code server (the pod that really needs the rule) is blocked."""
    (pod,) = [d for d in docs if d["kind"] == "Pod"]
    (deny,) = [d for d in docs if d["kind"] == "NetworkPolicy" and d["metadata"]["name"].endswith("-default-deny")]
    labels = pod["metadata"]["labels"]
    assert all(labels.get(k) == v for k, v in deny["spec"]["podSelector"]["matchLabels"].items())
    test = _policy(docs, "test")
    assert test["spec"].get("ingress") in (None, [])
    runner = _egress_targets(_policy(docs, "code-server"))["runner"]
    assert runner in test["spec"]["egress"]                                  # the very same rule
    assert sorted(_egress_targets(test)) == ["dns", "runner"]                # and nothing else (no Postgres, no Git)


# ---- DG-019: no ReadWriteOnce volume is shared by independently scheduled Deployments ------------------
# The chart used to mount ONE ReadWriteOnce compute-logs PVC and ONE artifacts PVC into the code server, the daemon AND
# the webserver. A ReadWriteOnce volume attaches to one node: if the scheduler put those pods on different nodes (or a
# rollout started a new pod before the old one released the volume) the pods stayed ContainerCreating with a Multi-Attach
# error. Now each volume goes only to the pods that use it, with an access mode that allows it.
def _pods(done):
    return {d["metadata"]["labels"]["app.kubernetes.io/component"]: d for d in _kinds(done, "Deployment")}


def _claims(deployment) -> set[str]:
    return {v["persistentVolumeClaim"]["claimName"] for v in deployment["spec"]["template"]["spec"]["volumes"]
            if "persistentVolumeClaim" in v}


def _volume(deployment, name) -> dict:
    return next(v for v in deployment["spec"]["template"]["spec"]["volumes"] if v["name"] == name)


S3 = ["persistence.computeLogs.mode=s3", "persistence.computeLogs.s3.bucket=acme-dagster-logs"]
CONFIGS = {
    "default-rwx": [],
    "s3": S3,
    "no-persistence": ["persistence.enabled=false"],
    "s3-no-persistence": [*S3, "persistence.enabled=false"],
}


@pytest.mark.parametrize("config", CONFIGS)
def test_no_readwriteonce_volume_is_shared_between_deployments(config):
    done = _render(*[a for kv in CONFIGS[config] for a in ("--set", kv)])
    pvcs = {p["metadata"]["name"]: p["spec"]["accessModes"] for p in _kinds(done, "PersistentVolumeClaim")}
    deployments = _pods(done)
    for claim, modes in pvcs.items():
        users = [name for name, d in deployments.items() if claim in _claims(d)]
        if "ReadWriteMany" not in modes:
            assert len(users) <= 1, f"{claim} is {modes} but mounted by {users}"
            for u in users:        # one pod, and a rollout must not start its replacement while it still holds the volume
                assert deployments[u]["spec"]["replicas"] == 1
                assert deployments[u]["spec"]["strategy"] == {"type": "Recreate"}


def test_the_compute_log_volume_is_readwritemany_and_shared_by_exactly_the_code_server_and_webserver():
    done = _render()
    (logs,) = [p for p in _kinds(done, "PersistentVolumeClaim") if p["metadata"]["name"].endswith("-compute-logs")]
    assert logs["spec"]["accessModes"] == ["ReadWriteMany"] and logs["spec"]["storageClassName"] == "nfs-rwx"
    deployments = _pods(done)
    assert {n for n, d in deployments.items() if logs["metadata"]["name"] in _claims(d)} == {"code-server", "webserver"}
    assert _volume(deployments["webserver"], "compute-logs") == _volume(deployments["code-server"], "compute-logs")


def test_the_artifacts_volume_belongs_to_the_code_server_alone_and_is_readwriteonce():
    done = _render()
    (art,) = [p for p in _kinds(done, "PersistentVolumeClaim") if p["metadata"]["name"].endswith("-artifacts")]
    assert art["spec"]["accessModes"] == ["ReadWriteOnce"]
    deployments = _pods(done)
    assert {n for n, d in deployments.items() if art["metadata"]["name"] in _claims(d)} == {"code-server"}
    assert deployments["code-server"]["spec"]["strategy"] == {"type": "Recreate"}


def test_the_daemon_mounts_no_persistent_volume_but_still_has_the_paths():
    daemon = _pods(_render())["daemon"]
    assert _claims(daemon) == set()
    mounts = {m["name"] for m in daemon["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]}
    assert {"compute-logs", "artifacts"} <= mounts                                    # the instance still creates its directories
    assert _volume(daemon, "compute-logs") == {"name": "compute-logs", "emptyDir": {}}


def test_the_webserver_does_not_mount_the_artifacts_volume():
    assert _volume(_pods(_render())["webserver"], "artifacts") == {"name": "artifacts", "emptyDir": {}}


def test_a_readwritemany_class_is_required_not_guessed():
    done = subprocess.run(["helm", "template", "d", str(CHART), "--set", "postgresql.external.host=db.example",
                           "--set", "tenant.orgId=acme"], capture_output=True, text=True, check=False)
    assert done.returncode != 0 and "ReadWriteMany" in done.stderr and "persistence.computeLogs.storageClass" in done.stderr


def test_object_storage_for_compute_logs_needs_no_pvc_and_configures_the_s3_manager():
    done = _render(*[a for kv in S3 for a in ("--set", kv)], "--set", "persistence.computeLogs.s3.prefix=acme/logs",
                   "--set", "persistence.computeLogs.s3.region=eu-west-1")
    assert not [p for p in _kinds(done, "PersistentVolumeClaim") if p["metadata"]["name"].endswith("-compute-logs")]
    (cm,) = [c for c in _kinds(done, "ConfigMap") if c["metadata"]["name"].endswith("-config")]
    cfg = yaml.safe_load(cm["data"]["dagster.yaml"])["compute_logs"]
    assert cfg["module"] == "dagster_aws.s3.compute_log_manager" and cfg["class"] == "S3ComputeLogManager"
    assert cfg["config"]["bucket"] == "acme-dagster-logs" and cfg["config"]["prefix"] == "acme/logs"
    assert cfg["config"]["region"] == "eu-west-1" and cfg["config"]["local_dir"] == "/var/lib/dagster/compute-logs"
    for d in _pods(done).values():
        assert _volume(d, "compute-logs") == {"name": "compute-logs", "emptyDir": {}}  # only local staging; S3 is the shared store


def test_the_default_keeps_the_local_compute_log_manager():
    (cm,) = [c for c in _kinds(_render(), "ConfigMap") if c["metadata"]["name"].endswith("-config")]
    assert yaml.safe_load(cm["data"]["dagster.yaml"])["compute_logs"]["class"] == "LocalComputeLogManager"


def test_s3_credentials_reach_only_the_pods_that_write_or_read_logs():
    done = _render(*[a for kv in S3 for a in ("--set", kv)], "--set", "persistence.computeLogs.s3.credentialsSecretRef.name=s3-creds")
    holders = {n for n, d in _pods(done).items()
               if any(e["name"] == "AWS_SECRET_ACCESS_KEY" for e in d["spec"]["template"]["spec"]["containers"][0]["env"])}
    assert holders == {"code-server", "webserver"}
    env = {e["name"]: e for e in _pods(done)["code-server"]["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["AWS_SECRET_ACCESS_KEY"]["valueFrom"]["secretKeyRef"]["name"] == "s3-creds"


@pytest.mark.parametrize("sets,expected", [
    (["persistence.computeLogs.mode=s3"], "persistence.computeLogs.s3.bucket"),
    (["persistence.computeLogs.mode=nfs"], "must be rwx or s3"),
    (["codeServer.replicaCount=2"], "ReadWriteOnce"),
], ids=["s3-without-bucket", "unknown-mode", "two-code-servers-on-one-rwo-volume"])
def test_unsafe_or_incomplete_storage_settings_are_refused(sets, expected):
    done = _render(*[a for kv in sets for a in ("--set", kv)])
    assert done.returncode != 0 and expected in done.stderr


def test_several_code_servers_are_allowed_once_nothing_readwriteonce_is_involved():
    done = _render(*[a for kv in [*S3, "persistence.enabled=false", "codeServer.replicaCount=2"] for a in ("--set", kv)])
    assert done.returncode == 0 and _pods(done)["code-server"]["spec"]["replicas"] == 2


def test_without_persistence_nothing_is_persistent_and_nothing_is_recreated():
    done = _render("--set", "persistence.enabled=false")
    assert _kinds(done, "PersistentVolumeClaim") == []
    for d in _pods(done).values():
        assert _claims(d) == set() and "strategy" not in d["spec"]


# ── DG-004: postgresql.enabled=true really deploys a database; with neither it and an external host the chart refuses to render ──
def _pg_render(*sets: str):
    cmd = ["helm", "template", "d", str(CHART), "--set", "tenant.orgId=acme", "--set", RWX_CLASS]
    for s in sets:
        cmd += ["--set", s]
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def _pg_docs(done):
    assert done.returncode == 0, done.stderr
    return [d for d in yaml.safe_load_all(done.stdout) if d]


def _pg_env(docs, component="daemon"):
    (dep,) = [d for d in _deployments(docs) if d["metadata"]["labels"]["app.kubernetes.io/component"] == component]
    return {e["name"]: e for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]}


def test_enabling_postgresql_deploys_a_statefulset_and_a_service_the_pods_point_at():
    docs = _pg_docs(_pg_render("postgresql.enabled=true"))
    (sts,) = [d for d in docs if d["kind"] == "StatefulSet"]
    (svc,) = [d for d in docs if d["kind"] == "Service" and d["metadata"]["labels"].get("app.kubernetes.io/component") == "postgresql"]
    for component in ("code-server", "daemon", "webserver"):
        env = _pg_env(docs, component)
        assert env["DAGSTER_PG_HOST"]["value"] == svc["metadata"]["name"] == sts["spec"]["serviceName"]       # the host it points at EXISTS
        assert env["DAGSTER_PG_PORT"]["value"] == "5432"
    # the Service selects the StatefulSet's pods, on the port the database listens on
    assert svc["spec"]["selector"] == sts["spec"]["selector"]["matchLabels"] == sts["spec"]["template"]["metadata"]["labels"]
    assert svc["spec"]["ports"][0]["port"] == 5432 and sts["spec"]["template"]["spec"]["containers"][0]["ports"][0]["containerPort"] == 5432


def test_the_in_cluster_database_has_a_volume_and_takes_its_password_from_the_secret_never_a_literal():
    docs = _pg_docs(_pg_render("postgresql.enabled=true", "postgresql.internal.persistence.size=20Gi",
                            "postgresql.internal.persistence.storageClass=fast"))
    (sts,) = [d for d in docs if d["kind"] == "StatefulSet"]
    claim = sts["spec"]["volumeClaimTemplates"][0]
    assert claim["spec"]["resources"]["requests"]["storage"] == "20Gi" and claim["spec"]["storageClassName"] == "fast"
    c = sts["spec"]["template"]["spec"]["containers"][0]
    assert {m["name"]: m["mountPath"] for m in c["volumeMounts"]}["data"] == "/var/lib/postgresql/data"
    pw = next(e for e in c["env"] if e["name"] == "POSTGRES_PASSWORD")
    assert "value" not in pw and pw["valueFrom"]["secretKeyRef"] == {"name": "dagster-pg-secret", "key": "password"}
    env = {e["name"]: e.get("value") for e in c["env"]}
    assert env["POSTGRES_USER"] == "dagster" and env["POSTGRES_DB"] == "dagster"                               # what the Dagster pods log in as


def test_the_in_cluster_database_runs_unprivileged():
    docs = _pg_docs(_pg_render("postgresql.enabled=true"))
    (sts,) = [d for d in docs if d["kind"] == "StatefulSet"]
    pod = sts["spec"]["template"]["spec"]
    assert pod["securityContext"]["runAsNonRoot"] is True and pod["securityContext"]["runAsUser"] == 70
    sc = pod["containers"][0]["securityContext"]
    assert sc["allowPrivilegeEscalation"] is False and sc["readOnlyRootFilesystem"] is True and sc["capabilities"]["drop"] == ["ALL"]


def test_only_the_dagster_components_can_reach_the_in_cluster_database_and_they_are_let_out_to_it():
    docs = _pg_docs(_pg_render("postgresql.enabled=true"))
    policies = {d["metadata"]["name"]: d for d in docs if d["kind"] == "NetworkPolicy"}
    pg = policies["d-dagster-dbt-runner-postgresql"]
    assert pg["spec"]["egress"] == [] and "Egress" in pg["spec"]["policyTypes"]
    (rule,) = pg["spec"]["ingress"]
    assert sorted(p["podSelector"]["matchLabels"]["app.kubernetes.io/component"] for p in rule["from"]) == ["code-server", "daemon", "webserver"]
    assert rule["ports"] == [{"protocol": "TCP", "port": 5432}]
    for component in ("webserver", "daemon", "code"):                                                         # each component may reach it
        out = policies[f"d-dagster-dbt-runner-{component}"]["spec"]["egress"]
        assert any(r.get("to") == [{"podSelector": {"matchLabels": {
            "app.kubernetes.io/name": "dagster-dbt-runner", "app.kubernetes.io/instance": "d", "app.kubernetes.io/component": "postgresql"}}}]
            for r in out), component


def test_with_an_external_database_nothing_is_deployed_for_postgres():
    docs = _pg_docs(_pg_render("postgresql.external.host=db.example"))
    assert not [d for d in docs if d["kind"] == "StatefulSet"]
    assert _pg_env(docs)["DAGSTER_PG_HOST"]["value"] == "db.example"
    assert not [d for d in docs if d["metadata"]["name"].endswith("-postgresql")]
    policies = {d["metadata"]["name"]: d for d in docs if d["kind"] == "NetworkPolicy"}
    out = policies["d-dagster-dbt-runner-daemon"]["spec"]["egress"]
    assert any(r.get("to") == [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "dp-shared"}}}] for r in out)


def test_neither_an_external_host_nor_the_in_cluster_database_refuses_to_render():
    done = _pg_render()
    assert done.returncode != 0 and "postgresql.external.host is required" in done.stderr


def test_a_missing_password_secret_name_refuses_to_render_in_both_modes():
    for sets in (["postgresql.enabled=true"], ["postgresql.external.host=db.example"]):
        done = _pg_render(*sets, "postgresql.external.passwordSecretRef.name=")
        assert done.returncode != 0 and "passwordSecretRef.name is required" in done.stderr
