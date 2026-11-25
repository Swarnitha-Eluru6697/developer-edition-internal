"""DBT-001: the dbt Runner enforces permissions itself, not only scopes.

A valid token for the right audience with the right scope is not enough: the token must also carry the PERMISSION the route
needs for the job's kind (workspace:dbt:edit / workspace:dbt:run, and the sql-jobs / python-jobs pairs), so a same-org token
without it cannot operate the runner directly, whether it arrives as a delegated Workspace token or as an external-CI
service-principal token. Role names grant nothing: the runner never reads them (contract 01-CONTRACT.md).
"""
from __future__ import annotations

import time
import uuid

import httpx
import pytest
from jose import jwt

from app.core.auth import Principal
from app.core.config import get_settings
from app.core.permissions import PERMISSIONS, SCOPE_PERMISSIONS, kinds_allowed, may
from app.main import app
from app.models import Project, Run
from tests.perms import ALL_JOB, EDIT, RUN

SECRET = "workspace-to-dbtrunner-delegation-secret"
PID, RID = str(uuid.uuid4()), str(uuid.uuid4())

# (method, path template, scope the route needs, the dbt-kind permission the route needs)
ROUTES = [
    ("GET", "/api/v1/projects", "dbt.projects.read", "workspace:dbt:edit"),
    ("POST", "/api/v1/projects/register", "dbt.projects.write", "workspace:dbt:edit"),
    ("GET", f"/api/v1/projects/{PID}", "dbt.projects.read", "workspace:dbt:edit"),
    ("PATCH", f"/api/v1/projects/{PID}", "dbt.projects.write", "workspace:dbt:edit"),
    ("DELETE", f"/api/v1/projects/{PID}", "dbt.projects.write", "workspace:dbt:edit"),
    ("POST", f"/api/v1/projects/{PID}/run", "dbt.runs.trigger", "workspace:dbt:run"),
    ("GET", f"/api/v1/projects/{PID}/runs", "dbt.runs.read", "workspace:dbt:run"),
    ("GET", f"/api/v1/projects/{PID}/runs/{RID}", "dbt.runs.read", "workspace:dbt:run"),
    ("POST", f"/api/v1/projects/{PID}/runs/{RID}/cancel", "dbt.runs.trigger", "workspace:dbt:run"),
    ("GET", f"/api/v1/projects/{PID}/runs/{RID}/events", "dbt.runs.read", "workspace:dbt:run"),
    ("GET", f"/api/v1/projects/{PID}/runs/{RID}/manifest", "dbt.runs.read", "workspace:dbt:run"),
    ("GET", f"/api/v1/projects/{PID}/lineage", "dbt.projects.read", "workspace:dbt:edit"),
]
# Holders that pass every route (for some kind) and holders that pass none.
ALLOWED = [ALL_JOB, EDIT + RUN[:1], ["workspace:dbt:edit", "workspace:dbt:run"]]
DENIED = [[], ["workspace:flink:deploy", "workspace:sql:run"], ["mlstudio:train:run", "ucs:rbac:manage"],
          ["workspace:dbt"], ["WORKSPACE:DBT:RUN"]]


@pytest.fixture(autouse=True)
def delegation(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "delegation_jwt_secret", SECRET)
    monkeypatch.setattr(s, "delegation_jwt_allowed_issuers", ["workspace"])
    monkeypatch.setattr(s, "control_plane_audience", "dbtrunner")


def delegated(scope, permissions, **extra) -> str:
    now = int(time.time())
    return jwt.encode({"iss": "workspace", "act": {"sub": "workspace"}, "sub": "user-1", "aud": "dbtrunner", "scope": scope,
                       "org_id": get_settings().org_id, "permissions": list(permissions), "jti": uuid.uuid4().hex,
                       "iat": now, "exp": now + 60, **extra}, SECRET, algorithm="HS256")


@pytest.fixture
async def api(run_db):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _call(api, method, path, token):
    return await api.request(method, path, headers={"Authorization": f"Bearer {token}"},
                             json={} if method in ("POST", "PATCH") else None)


def _p(*permissions) -> Principal:
    return Principal(user_id="u", org_id="o", permissions=frozenset(permissions), email=None, issued_at=0, expires_at=0)


# ── the permission table ────────────────────────────────────────────────
@pytest.mark.parametrize("held,read,write,runs", [
    (ALL_JOB, True, True, True),
    (["workspace:dbt:edit"], True, True, False),
    (["workspace:dbt:run"], True, False, True),             # may read a job it may run
    (["workspace:sql-jobs:run"], True, False, True),         # for SQL jobs only (see kinds below)
    ([], False, False, False),
    (["workspace:flink:deploy"], False, False, False),
    (["workspace:dbt:RUN"], False, False, False),           # IDs are exact, never case-folded
])
def test_permission_table(held, read, write, runs):
    p = _p(*held)
    assert may(p, "dbt.projects.read") is read
    assert may(p, "dbt.projects.write") is write
    assert may(p, "dbt.runs.trigger") is runs and may(p, "dbt.runs.read") is runs


def test_each_job_kind_needs_its_own_permission():
    p = _p("workspace:sql-jobs:edit", "workspace:python-jobs:run")
    assert kinds_allowed(p, "dbt.projects.write") == ["sql"]
    assert kinds_allowed(p, "dbt.runs.trigger") == ["python"]
    assert kinds_allowed(p, "dbt.projects.read") == ["sql", "python"]
    assert not may(p, "dbt.runs.trigger", "dbt") and not may(p, "dbt.projects.write", "dbt")


def test_the_table_names_only_workspace_job_permissions():
    assert PERMISSIONS == frozenset(ALL_JOB)
    assert set(SCOPE_PERMISSIONS) == {"dbt.projects.read", "dbt.projects.write", "dbt.runs.read", "dbt.runs.trigger"}
    assert set(SCOPE_PERMISSIONS["dbt.projects.write"]) == set(EDIT)
    assert set(SCOPE_PERMISSIONS["dbt.runs.trigger"]) == set(RUN)


def test_an_unknown_scope_is_a_programming_error():
    from app.core.deps import require_scope
    with pytest.raises(ValueError):
        require_scope("dbt.delete.everything")


# ── delegated Workspace tokens ───────────────────────────────────────────
@pytest.mark.parametrize("method,path,scope,_cap", ROUTES, ids=lambda v: v if isinstance(v, str) and v.startswith("dbt.") else None)
@pytest.mark.parametrize("held", DENIED, ids=lambda r: ",".join(r) or "no-permissions")
async def test_a_token_without_the_permission_is_forbidden_on_every_route(api, method, path, scope, _cap, held):
    r = await _call(api, method, path, delegated(scope, held))
    assert r.status_code == 403, (method, path, r.text)
    assert r.json()["detail"]["code"] == "FORBIDDEN"


@pytest.mark.parametrize("method,path,scope,_cap", ROUTES, ids=lambda v: v if isinstance(v, str) and v.startswith("dbt.") else None)
@pytest.mark.parametrize("held", ALLOWED, ids=lambda r: ",".join(r))
async def test_a_token_with_the_permission_passes_authorisation(api, method, path, scope, _cap, held):
    r = await _call(api, method, path, delegated(scope, held))
    assert r.status_code != 403 and r.status_code != 401, (method, path, r.text)   # 200/404/422: past authorisation


@pytest.mark.parametrize("forged", [{"roles": ["workspace_admin", "admin"]}, {"role": "workspace_admin"},
                                    {"realm_access": {"roles": ["workspace_admin"]}}])
async def test_role_claims_grant_nothing(api, forged):
    r = await _call(api, "POST", f"/api/v1/projects/{PID}/run", delegated("dbt.runs.trigger", [], **forged))
    assert r.status_code == 403 and r.json()["detail"]["code"] == "FORBIDDEN"


async def test_scope_is_checked_before_the_permission(api):
    """Both are wrong (no scope, no permission): the token-level problem is reported first."""
    r = await _call(api, "GET", "/api/v1/projects", delegated("dbt.runs.trigger", []))
    assert r.status_code == 403 and r.json()["detail"]["code"] == "INSUFFICIENT_SCOPE"


# ── external CI service-principal tokens ─────────────────────────────────
async def test_a_ci_token_with_scope_but_no_permission_is_forbidden(api, mint_jwt):
    t = mint_jwt(audience="dbtrunner", scopes=["dbt.runs.trigger"], permissions=["workspace:dbt:edit"])
    r = await _call(api, "POST", f"/api/v1/projects/{PID}/run", t)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "FORBIDDEN"


async def test_a_ci_token_with_scope_and_permission_passes_authorisation(api, mint_jwt):
    t = mint_jwt(audience="dbtrunner", scopes=["dbt.projects.read"], permissions=["workspace:dbt:edit"])
    assert (await _call(api, "GET", "/api/v1/projects", t)).status_code == 200


# ── edge cases ───────────────────────────────────────────────────────────
async def test_a_denied_trigger_starts_no_run_and_touches_nothing(api, run_db):
    async with run_db() as db:
        p = Project(org_id=get_settings().org_id, name="analytics", git_url="https://github.com/acme/a.git",
                    git_ref="main", engine="pg_lake")
        db.add(p)
        await db.commit()
        pid = p.id
    r = await _call(api, "POST", f"/api/v1/projects/{pid}/run", delegated("dbt.runs.trigger", ["workspace:dbt:edit"]))
    assert r.status_code == 403
    async with run_db() as db:
        from sqlalchemy import func, select
        assert (await db.execute(select(func.count()).select_from(Run))).scalar() == 0


async def test_a_denied_caller_cannot_tell_whether_a_project_exists(api):
    """Authorisation comes before the lookup: a caller with no job permission gets 403 for a missing project, not 404."""
    r = await _call(api, "GET", f"/api/v1/projects/{uuid.uuid4()}", delegated("dbt.projects.read", []))
    assert r.status_code == 403


async def test_the_decision_is_stable_when_repeated(api):
    for _ in range(3):
        r = await _call(api, "GET", "/api/v1/projects", delegated("dbt.projects.read", []))
        assert r.status_code == 403


async def test_concurrent_requests_are_each_decided_on_their_own_token(api):
    import asyncio
    ok = [_call(api, "GET", "/api/v1/projects", delegated("dbt.projects.read", ALL_JOB)) for _ in range(5)]
    no = [_call(api, "GET", "/api/v1/projects", delegated("dbt.projects.read", [])) for _ in range(5)]
    got = await asyncio.gather(*ok, *no)
    assert [r.status_code for r in got] == [200] * 5 + [403] * 5


async def test_the_scheduler_identity_needs_no_user_permissions(api):
    """Dagster's /internal routes (DBT-003: its own scoped identity) carry no user permissions and must keep working."""
    s = get_settings()
    s.scheduler_jwt_secret = "dagster-scheduler-signing-secret"
    now = int(time.time())
    t = jwt.encode({"iss": "dagster", "sub": "dagster", "aud": "dbtrunner", "scope": "dbt.projects.read",
                    "org_id": s.org_id, "jti": uuid.uuid4().hex, "iat": now, "exp": now + 60},
                   s.scheduler_jwt_secret, algorithm="HS256")
    r = await api.get("/api/v1/internal/projects", headers={"X-Internal-Service-Token": t})
    assert r.status_code == 200


# ── every user route demands a scope AND a permission (drift guard) ──────
def _user_routes():
    # Walk the router itself: `app.routes` wraps included routers lazily in newer FastAPI and would list none.
    from app.api.projects import router
    for r in router.routes:
        path = getattr(r, "path", "")
        if "/internal" not in path and not path.endswith("/schema-tests") and path not in ("/api/v1/validate",):
            yield r


def test_the_drift_guard_actually_sees_the_user_routes():
    """A guard that collects nothing passes vacuously (pytest skips an empty parameter set)."""
    assert len(list(_user_routes())) == len(ROUTES)


@pytest.mark.parametrize("route", list(_user_routes()), ids=lambda r: f"{min(r.methods)} {r.path}")
def test_every_user_route_requires_a_scope_and_its_job_kind_permission(route):
    import inspect
    calls = [d.call for d in route.dependant.dependencies]
    scopes = {getattr(c, "required_scope", None) for c in calls} - {None}
    assert len(scopes) == 1, (route.path, scopes)
    table = {(m, p.replace(PID, "{project_id}").replace(RID, "{run_id}")): s for m, p, s, _ in ROUTES}
    scope = next(iter(scopes))
    assert table[(min(route.methods), route.path)] == scope
    src = inspect.getsource(route.endpoint)
    assert "ensure_job_permission(user, SCOPE_" in src, f"{route.path}: no per-kind permission check"


# ── kind-specific enforcement against real rows ─────────────────────────
async def _project(run_db, kind="dbt"):
    async with run_db() as db:
        p = Project(org_id=get_settings().org_id, name=f"job-{uuid.uuid4().hex[:6]}", git_url="https://github.com/acme/a.git",
                    git_ref="main", engine="pg_lake", kind=kind, project_path="a.sql" if kind != "dbt" else None)
        db.add(p)
        await db.commit()
        return p.id


async def test_a_sql_job_permission_does_not_run_a_dbt_project(api, run_db):
    pid = await _project(run_db, "dbt")
    r = await _call(api, "POST", f"/api/v1/projects/{pid}/run", delegated("dbt.runs.trigger", ["workspace:sql-jobs:run"]))
    assert r.status_code == 403 and "workspace:dbt:run" in r.json()["detail"]["message"]


async def test_the_list_shows_only_the_kinds_the_caller_holds(api, run_db):
    dbt_id, sql_id = await _project(run_db, "dbt"), await _project(run_db, "sql")
    r = await _call(api, "GET", "/api/v1/projects", delegated("dbt.projects.read", ["workspace:sql-jobs:edit"]))
    ids = {p["id"] for p in r.json()}
    assert str(sql_id) in ids and str(dbt_id) not in ids
    r = await _call(api, "GET", "/api/v1/projects?kind=dbt", delegated("dbt.projects.read", ["workspace:sql-jobs:edit"]))
    assert r.status_code == 403
