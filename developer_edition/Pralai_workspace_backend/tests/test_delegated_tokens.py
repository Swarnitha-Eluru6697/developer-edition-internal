"""X-001: Workspace calls the dbt and FlinkSQL runners with a short-lived DELEGATED token, never the user's bearer.

Target pattern:  Browser --(HttpOnly session / trusted gateway identity)--> Workspace
                 Workspace --(HS256, aud=<runner>, ONE scope, sub=user, act=workspace, the scope's permissions, short-lived)--> runner
The user's own credential (Authorization header or session cookie) must not reach a runner at all, and the token carries the
caller's PERMISSIONS that the scope needs — never role names (contract 01-CONTRACT.md).
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import httpx
import pytest
from conftest import subject_for
from jose import jwt

from app.api import dbt_proxy, flink_proxy
from app.core.config import get_settings
from app.core.service_tokens import DBTRUNNER_AUDIENCE, FLINK_AUDIENCE, SCOPE_PERMISSIONS, mint_delegated_token
from role_seed import permissions_for

DBT_SECRET = "workspace-to-dbtrunner-delegation-secret"
FLINK_SECRET = "workspace-to-flink-delegation-secret"
BROWSER_BEARER = "browser.session.credential-that-must-never-reach-a-runner"
PID, RID, JID = (str(uuid.uuid4()) for _ in range(3))
ROLES = ["data_engineer"]          # test seed: holds the dbt, job, Flink and catalog permissions
HELD = set(permissions_for(ROLES))


def _expected_permissions(scope: str) -> list[str]:
    return sorted(HELD & set(SCOPE_PERMISSIONS[scope]))

DBT_ROUTES = [
    ("GET", "/api/v1/dbt/projects", "dbt.projects.read"),
    ("POST", "/api/v1/dbt/projects/register", "dbt.projects.write"),
    ("GET", f"/api/v1/dbt/projects/{PID}", "dbt.projects.read"),
    ("PATCH", f"/api/v1/dbt/projects/{PID}", "dbt.projects.write"),
    ("DELETE", f"/api/v1/dbt/projects/{PID}", "dbt.projects.write"),
    ("POST", f"/api/v1/dbt/projects/{PID}/run", "dbt.runs.trigger"),
    ("GET", f"/api/v1/dbt/projects/{PID}/runs", "dbt.runs.read"),
    ("GET", f"/api/v1/dbt/projects/{PID}/runs/{RID}", "dbt.runs.read"),
    ("GET", f"/api/v1/dbt/projects/{PID}/runs/{RID}/events", "dbt.runs.read"),
    ("GET", f"/api/v1/dbt/projects/{PID}/lineage", "dbt.projects.read"),
]
FLINK_ROUTES = [
    ("GET", "/api/v1/flink/jobs", "flink.jobs.read"),
    ("POST", "/api/v1/flink/jobs", "flink.jobs.edit"),
    ("GET", f"/api/v1/flink/jobs/{JID}", "flink.jobs.read"),
    ("PATCH", f"/api/v1/flink/jobs/{JID}", "flink.jobs.edit"),
    ("DELETE", f"/api/v1/flink/jobs/{JID}", "flink.jobs.edit"),
    ("POST", f"/api/v1/flink/jobs/{JID}/validate", "flink.jobs.edit"),
    ("POST", f"/api/v1/flink/jobs/{JID}/deploy", "flink.jobs.deploy"),
    ("POST", f"/api/v1/flink/jobs/{JID}/stop", "flink.jobs.deploy"),
    ("POST", f"/api/v1/flink/jobs/{JID}/cancel", "flink.jobs.deploy"),
    ("GET", f"/api/v1/flink/jobs/{JID}/status", "flink.jobs.read"),
]


@pytest.fixture
def secrets(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "dbtrunner_delegation_jwt_secret", DBT_SECRET)
    monkeypatch.setattr(s, "flinksql_delegation_jwt_secret", FLINK_SECRET)
    monkeypatch.setattr(s, "dbtrunner_url", "http://dbt.test")
    monkeypatch.setattr(s, "flinksql_runner_url", "http://flink.test")
    return s


@pytest.fixture
def upstream(monkeypatch, secrets):
    """Fake dbt + Flink runners; yields the list of requests they received."""
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json=[] if req.method == "GET" and req.url.path.endswith(("jobs", "projects", "runs", "events")) else {})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    return seen


def _call(client, gateway_headers, method, path, **kw):
    headers = {**gateway_headers(user_name="alice", roles=ROLES), "Authorization": f"Bearer {BROWSER_BEARER}"}
    client.cookies.set("workspace_session", BROWSER_BEARER)
    try:
        return client.request(method, path, headers=headers, json={} if method in ("POST", "PATCH") else None, **kw)
    finally:
        client.cookies.clear()


def _claims(req: httpx.Request, secret: str, audience: str) -> dict:
    token = req.headers["Authorization"].removeprefix("Bearer ")
    return jwt.decode(token, secret, algorithms=["HS256"], audience=audience,
                      options={"require_exp": True, "require_iat": True, "require_jti": True})


# ── the token itself ─────────────────────────────────────────────────────
def test_the_delegated_token_carries_the_user_the_scope_its_permissions_and_workspace_as_actor(secrets):
    p = SimpleNamespace(user_id="alice", org_id=secrets.org_id, roles=["someone-elses-role-name"], email="a@x.io",
                        permissions=frozenset({"workspace:dbt:run", "workspace:dbt:edit", "workspace:sql:run",
                                               "mlstudio:train:run"}))
    c = jwt.decode(mint_delegated_token(secret=DBT_SECRET, audience=DBTRUNNER_AUDIENCE, scope="dbt.runs.trigger", principal=p),
                   DBT_SECRET, algorithms=["HS256"], audience=DBTRUNNER_AUDIENCE)
    assert (c["iss"], c["act"], c["sub"]) == ("workspace", {"sub": "workspace"}, "alice")
    assert c["scope"].split() == ["dbt.runs.trigger"]
    # only what this scope needs, out of what the caller holds; never the roles
    assert (c["org_id"], c["permissions"], c["email"]) == (secrets.org_id, ["workspace:dbt:run"], "a@x.io")
    assert "roles" not in c
    assert 0 < c["exp"] - c["iat"] <= 120


def test_a_permission_the_caller_does_not_hold_is_never_put_in_the_token(secrets):
    p = SimpleNamespace(user_id="alice", org_id=secrets.org_id, roles=[], email=None, permissions=frozenset({"workspace:dbt:edit"}))
    c = jwt.get_unverified_claims(mint_delegated_token(secret=DBT_SECRET, audience=DBTRUNNER_AUDIENCE,
                                                       scope="dbt.runs.trigger", principal=p))
    assert c["permissions"] == []


def test_every_token_has_a_fresh_jti(secrets):
    p = SimpleNamespace(user_id="alice", org_id=secrets.org_id, roles=[], email=None, permissions=frozenset())
    mint = lambda: jwt.get_unverified_claims(mint_delegated_token(secret=DBT_SECRET, audience="a", scope="s", principal=p))["jti"]
    assert mint() != mint()


# ── dbt runner ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("method,path,scope", DBT_ROUTES, ids=lambda v: v if isinstance(v, str) and v.startswith("dbt.") else None)
def test_dbt_proxy_sends_a_delegated_token_with_the_routes_scope(client, gateway_headers, upstream, secrets, method, path, scope):
    r = _call(client, gateway_headers, method, path)
    assert r.status_code < 400, r.text
    (req,) = upstream
    c = _claims(req, DBT_SECRET, DBTRUNNER_AUDIENCE)
    assert c["scope"].split() == [scope]
    assert (c["sub"], c["org_id"]) == (subject_for("alice"), secrets.org_id)   # the gateway-authenticated user
    assert c["permissions"] == _expected_permissions(scope) and c["permissions"]
    assert "roles" not in c


def test_the_browsers_own_credential_never_reaches_the_dbt_runner(client, gateway_headers, upstream):
    _call(client, gateway_headers, "GET", "/api/v1/dbt/projects")
    (req,) = upstream
    blob = repr(dict(req.headers)) + (req.content or b"").decode(errors="replace")
    assert BROWSER_BEARER not in blob
    assert "cookie" not in {k.lower() for k in req.headers}


def test_dbt_proxy_fails_closed_without_a_delegation_secret(client, gateway_headers, upstream, monkeypatch):
    monkeypatch.setattr(get_settings(), "dbtrunner_delegation_jwt_secret", None)
    r = _call(client, gateway_headers, "GET", "/api/v1/dbt/projects")
    assert r.status_code == 503 and r.json()["detail"]["code"] == "SERVICE_AUTH_NOT_CONFIGURED"
    assert upstream == [], "nothing may be sent to the runner, and above all not the user's credential"


# ── FlinkSQL runner ──────────────────────────────────────────────────────
@pytest.mark.parametrize("method,path,scope", FLINK_ROUTES, ids=lambda v: v if isinstance(v, str) and v.startswith("flink.") else None)
def test_flink_proxy_sends_a_delegated_token_with_the_routes_scope(client, gateway_headers, upstream, secrets, method, path, scope):
    r = _call(client, gateway_headers, method, path)
    assert r.status_code < 400, r.text
    (req,) = upstream
    c = _claims(req, FLINK_SECRET, FLINK_AUDIENCE)
    assert c["scope"].split() == [scope]
    assert (c["sub"], c["org_id"]) == (subject_for("alice"), secrets.org_id)
    assert c["permissions"] == _expected_permissions(scope) and c["permissions"]
    assert "roles" not in c


def test_the_browsers_own_credential_never_reaches_the_flink_runner(client, gateway_headers, upstream):
    _call(client, gateway_headers, "GET", "/api/v1/flink/jobs")
    (req,) = upstream
    assert BROWSER_BEARER not in repr(dict(req.headers))
    assert "cookie" not in {k.lower() for k in req.headers}


def test_flink_proxy_fails_closed_without_a_delegation_secret(client, gateway_headers, upstream, monkeypatch):
    monkeypatch.setattr(get_settings(), "flinksql_delegation_jwt_secret", None)
    r = _call(client, gateway_headers, "POST", f"/api/v1/flink/jobs/{JID}/deploy")
    assert r.status_code == 503
    assert upstream == []


def test_a_token_for_one_runner_is_useless_at_the_other(client, gateway_headers, upstream):
    _call(client, gateway_headers, "GET", "/api/v1/dbt/projects")
    with pytest.raises(jwt.JWTError):
        _claims(upstream[0], FLINK_SECRET, FLINK_AUDIENCE)


def test_sync_makes_two_calls_each_with_only_the_scope_it_needs(client, gateway_headers, monkeypatch, secrets):
    """GET the job (read) then PATCH it (edit): one token per call, one scope per token."""
    async def fake_fetch(**_kw):
        return "SELECT 1", "job.sql", "a" * 40, "main"
    monkeypatch.setattr(flink_proxy, "_fetch_sql_from_git", fake_fetch)
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        job = {"description": "x\n[flink-git] url=https://github.com/a/b.git ref=main path=job.sql sha=abc"}
        return httpx.Response(200, json=job if req.method == "GET" else {})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    r = _call(client, gateway_headers, "POST", f"/api/v1/flink/jobs/{JID}/sync")
    assert r.status_code == 200, r.text
    assert [(q.method, _claims(q, FLINK_SECRET, FLINK_AUDIENCE)["scope"]) for q in seen] == [
        ("GET", "flink.jobs.read"), ("PATCH", "flink.jobs.edit")]


def test_from_git_creates_the_job_with_the_edit_scope(client, gateway_headers, upstream, monkeypatch):
    async def fake_fetch(**_kw):
        return "SELECT 1", "job.sql", "a" * 40, "main"
    monkeypatch.setattr(flink_proxy, "_fetch_sql_from_git", fake_fetch)
    r = client.post("/api/v1/flink/jobs/from-git", json={"git_url": "https://github.com/a/b.git"},
                    headers=gateway_headers(user_name="alice", roles=ROLES))
    assert r.status_code < 400, r.text
    (req,) = upstream
    assert _claims(req, FLINK_SECRET, FLINK_AUDIENCE)["scope"] == "flink.jobs.edit"


# ── drift guard: every proxied route has an explicit scope ───────────────
def test_every_dbt_proxy_route_is_in_the_scope_table():
    routed = {(m, r.path) for r in dbt_proxy.router.routes for m in r.methods}
    table = {(m, p.replace(PID, "{project_id}").replace(RID, "{run_id}")) for m, p, _ in DBT_ROUTES}
    assert routed == table, f"unscoped or stale routes: {routed ^ table}"


def test_every_flink_proxy_route_is_in_the_scope_table_or_is_not_a_runner_call():
    not_runner_calls = {("GET", "/api/v1/flink/ui"), ("POST", "/api/v1/flink/validate")}   # no user bearer involved
    special = {("POST", "/api/v1/flink/jobs/from-git"), ("POST", "/api/v1/flink/jobs/{job_id}/sync")}
    routed = {(m, r.path) for r in flink_proxy.router.routes for m in r.methods}
    table = {(m, p.replace(JID, "{job_id}")) for m, p, _ in FLINK_ROUTES}
    assert routed - not_runner_calls - special == table, f"unscoped or stale routes: {(routed - not_runner_calls - special) ^ table}"


# ── cross-service drift guard ────────────────────────────────────────────
# The scope and audience names are a contract between three repos. Each repo tests its own side; this reads the
# runner repos (checked out next to this one, or at RUNNERS_REPO) and fails if the vocabularies ever diverge.
import os
import re
from pathlib import Path

RUNNERS = Path(os.environ.get("RUNNERS_REPO", Path(__file__).resolve().parents[2] / "Pralai_workspace_runners"))


def _constants(path: Path, prefix: str) -> set[str]:
    return set(re.findall(rf'^{prefix}\w*\s*=\s*"([a-z][a-z.]+)"', path.read_text(), re.MULTILINE))


@pytest.mark.skipif(not (RUNNERS / "pralai-dbt-runner").exists(), reason="runner repos not checked out next to this one")
def test_dbt_scopes_and_audience_match_the_dbt_runner():
    from app.core import service_tokens as st
    mine = {st.DBT_SCOPE_PROJECTS_READ, st.DBT_SCOPE_PROJECTS_WRITE, st.DBT_SCOPE_RUNS_READ, st.DBT_SCOPE_RUNS_TRIGGER}
    runner = RUNNERS / "pralai-dbt-runner" / "app"
    assert _constants(runner / "core" / "deps.py", "SCOPE_") == mine
    assert re.search(rf'^SERVICE_AUDIENCE\s*=\s*"{st.DBTRUNNER_AUDIENCE}"', (runner / "core" / "service_auth.py").read_text(), re.MULTILINE)


@pytest.mark.skipif(not (RUNNERS / "pralai-flinksql-runner").exists(), reason="runner repos not checked out next to this one")
def test_flink_scopes_and_audience_match_the_flink_runner():
    from app.core import service_tokens as st
    mine = {st.FLINK_SCOPE_JOBS_READ, st.FLINK_SCOPE_JOBS_EDIT, st.FLINK_SCOPE_JOBS_DEPLOY}
    runner = RUNNERS / "pralai-flinksql-runner" / "app"
    theirs = _constants(runner / "core" / "permissions.py", "SCOPE_")
    assert mine <= theirs                                    # the runner also has flink.catalog.read (not proxied by Workspace)
    assert theirs - mine == {"flink.catalog.read"}
    assert re.search(rf'^SERVICE_AUDIENCE\s*=\s*"{st.FLINK_AUDIENCE}"', (runner / "core" / "service_auth.py").read_text(), re.MULTILINE)
