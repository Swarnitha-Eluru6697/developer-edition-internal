"""DBT-003: Dagster gets its OWN scoped service identity; the static INTERNAL_SERVICE_TOKEN no longer opens the
scheduler routes.

Before, one shared static token opened /validate (Workspace's codegen loop) AND the five /internal routes Dagster
uses to enumerate projects and trigger/read runs, so a leak from either holder could start dbt runs. Now:

  * /internal/*  : a short-lived HS256 JWT signed with SCHEDULER_JWT_SECRET (a key of its own), iss/sub "dagster",
                   aud "dbtrunner", ONE scope per route (dbt.projects.read / dbt.runs.read / dbt.runs.trigger / dbt.runs.cancel).
  * /validate    : the static token, exactly as documented, and nothing else.
  * schema-tests : DQ's own scoped token (unchanged, tests/test_service_auth.py).
"""
from __future__ import annotations

import time
import uuid

import httpx
import pytest
from jose import jwt

from tests.perms import ALL_JOB
from app.core.config import Settings, get_settings
from app.main import app

SCHEDULER_SECRET = "dagster-scheduler-signing-secret"
STATIC = "legacy-static-internal-token"
PID, RID = str(uuid.uuid4()), str(uuid.uuid4())

ROUTES = [
    ("GET", "/api/v1/internal/projects", "dbt.projects.read"),
    ("GET", "/api/v1/internal/projects/scheduled", "dbt.projects.read"),
    ("GET", "/api/v1/internal/projects/git-triggered", "dbt.projects.read"),       # DG-008
    ("GET", f"/api/v1/internal/projects/{PID}", "dbt.projects.read"),
    ("GET", f"/api/v1/internal/projects/{PID}/head", "dbt.projects.read"),            # DG-009
    ("POST", f"/api/v1/internal/projects/{PID}/run", "dbt.runs.trigger"),
    ("GET", f"/api/v1/internal/runs/{RID}", "dbt.runs.read"),
    ("POST", f"/api/v1/internal/runs/{RID}/cancel", "dbt.runs.cancel"),                # DG-017
    ("POST", f"/api/v1/internal/projects/{PID}/runs/cancel", "dbt.runs.cancel"),
]
READ = ROUTES[0]


@pytest.fixture(autouse=True)
def identities(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "scheduler_jwt_secret", SCHEDULER_SECRET)
    monkeypatch.setattr(s, "scheduler_jwt_allowed_issuers", ["dagster"])
    monkeypatch.setattr(s, "internal_service_token", STATIC)
    monkeypatch.setattr(s, "service_jwt_secret", "dq-service-secret")
    monkeypatch.setattr(s, "delegation_jwt_secret", "workspace-delegation-secret")
    return s


def token(scope="dbt.projects.read", *, secret=SCHEDULER_SECRET, drop=(), **over) -> str:
    now = int(time.time())
    claims = {"iss": "dagster", "sub": "dagster", "aud": "dbtrunner", "scope": scope,
              "org_id": get_settings().org_id, "jti": uuid.uuid4().hex, "iat": now, "exp": now + 60}
    claims.update(over)
    for k in drop:
        claims.pop(k, None)
    return jwt.encode(claims, secret, algorithm="HS256")


@pytest.fixture
async def api(run_db):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def call(api, method, path, tok):
    headers = {"X-Internal-Service-Token": tok} if tok is not None else {}
    return await api.request(method, path, headers=headers, json={} if method == "POST" else None)


def authorised(r) -> bool:
    return r.status_code not in (401, 403)       # past authentication/authorisation (what follows is the DB)


def code(r) -> str | None:
    try:
        return r.json()["detail"]["code"]
    except Exception:  # noqa: BLE001
        return None


# ── accepted ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("method,path,scope", ROUTES, ids=lambda v: v if isinstance(v, str) and v.startswith("dbt.") else None)
async def test_the_scheduler_identity_with_the_routes_scope_is_accepted(api, method, path, scope):
    assert authorised(await call(api, method, path, token(scope)))


async def test_a_read_token_can_be_presented_again(api):
    t = token("dbt.projects.read")
    assert (await call(api, "GET", READ[1], t)).status_code == 200
    assert (await call(api, "GET", READ[1], t)).status_code == 200


# ── the static token is no longer a scheduler credential ─────────────────
@pytest.mark.parametrize("method,path,scope", ROUTES, ids=lambda v: v if isinstance(v, str) and v.startswith("dbt.") else None)
async def test_the_raw_static_token_is_refused_on_every_scheduler_route(api, method, path, scope):
    r = await call(api, method, path, STATIC)
    assert r.status_code == 401 and code(r) == "RAW_TOKEN_REJECTED"


async def test_a_refused_static_token_does_not_start_a_run(api, run_db):
    from sqlalchemy import func, select

    from app.models import Run
    await call(api, "POST", f"/api/v1/internal/projects/{PID}/run", STATIC)
    async with run_db() as db:
        assert (await db.execute(select(func.count()).select_from(Run))).scalar() == 0


async def test_the_static_token_still_works_on_validate_its_documented_purpose(api):
    r = await api.post("/api/v1/validate", headers={"X-Internal-Service-Token": STATIC}, json={})
    assert r.status_code != 401                                     # 422: bad body, but authenticated


async def test_a_scheduler_token_does_not_open_validate(api):
    r = await api.post("/api/v1/validate", headers={"X-Internal-Service-Token": token()}, json={})
    assert r.status_code == 401


# ── scopes are separate ──────────────────────────────────────────────────
async def test_a_read_scope_cannot_trigger_a_run(api):
    r = await call(api, "POST", f"/api/v1/internal/projects/{PID}/run", token("dbt.projects.read"))
    assert r.status_code == 403 and code(r) == "INSUFFICIENT_SCOPE"


async def test_a_trigger_scope_cannot_enumerate_projects(api):
    r = await call(api, "GET", READ[1], token("dbt.runs.trigger"))
    assert r.status_code == 403 and code(r) == "INSUFFICIENT_SCOPE"


async def test_a_run_read_scope_cannot_read_projects(api):
    assert (await call(api, "GET", READ[1], token("dbt.runs.read"))).status_code == 403


async def test_a_token_with_several_scopes_is_still_checked_per_route(api):
    t = token("dbt.projects.read dbt.runs.read")
    assert authorised(await call(api, "GET", READ[1], t))
    assert (await call(api, "POST", f"/api/v1/internal/projects/{PID}/run", t)).status_code == 403


# ── other trust domains' credentials mean nothing here ───────────────────
@pytest.mark.parametrize("secret", ["dq-service-secret", "workspace-delegation-secret", STATIC, "some-other-key"],
                         ids=["dq-key", "workspace-delegation-key", "static-token-as-key", "unknown-key"])
async def test_a_token_signed_with_another_identitys_key_is_refused(api, secret):
    assert (await call(api, "GET", READ[1], token(secret=secret))).status_code == 401


@pytest.mark.parametrize("who", ["dataquality", "workspace", "dbtrunner"])
async def test_only_dagster_is_an_allowed_issuer(api, who):
    r = await call(api, "GET", READ[1], token(iss=who, sub=who))
    assert r.status_code == 401


async def test_a_user_jwt_is_not_a_scheduler_credential(api, mint_jwt):
    r = await call(api, "GET", READ[1], mint_jwt(audience="dbtrunner", scopes=["dbt.projects.read"], permissions=ALL_JOB))
    assert r.status_code == 401


# ── invalid tokens ───────────────────────────────────────────────────────
@pytest.mark.parametrize("make", [
    lambda: "not-a-jwt",
    lambda: token(aud="somebody-else"),
    lambda: token(exp=int(time.time()) - 120, iat=int(time.time()) - 180),
    lambda: token(iat=int(time.time()) + 600, exp=int(time.time()) + 660),
    lambda: token(exp=int(time.time()) + 3600),
    lambda: token(drop=("jti",)),
    lambda: token(drop=("sub",)),
    lambda: token(drop=("iss",)),
    lambda: token(drop=("org_id",)),
    lambda: token(drop=("exp",)),
    lambda: token(sub="someone-else"),
    lambda: token(scope=""),
], ids=["malformed", "wrong-audience", "expired", "future-iat", "long-lived", "no-jti", "no-sub", "no-iss",
        "no-org", "no-exp", "subject-not-allowed", "no-scope"])
async def test_an_invalid_token_is_refused(api, make):
    r = await call(api, "GET", READ[1], make())
    assert r.status_code in (401, 403) and r.status_code != 200


async def test_a_token_for_another_org_is_forbidden(api):
    r = await call(api, "GET", READ[1], token(org_id="some-other-org"))
    assert r.status_code == 403 and code(r) == "ORG_MISMATCH"


async def test_a_missing_token_is_refused(api):
    assert (await call(api, "GET", READ[1], None)).status_code == 401


async def test_without_a_configured_secret_nothing_is_accepted_not_even_the_static_token(api, monkeypatch):
    monkeypatch.setattr(get_settings(), "scheduler_jwt_secret", None)
    for tok in (token(), STATIC):
        r = await call(api, "GET", READ[1], tok)
        assert r.status_code == 401 and code(r) == "SERVICE_AUTH_NOT_CONFIGURED"


# ── replay ───────────────────────────────────────────────────────────────
async def test_a_trigger_token_works_once_then_is_a_replay(api):
    t = token("dbt.runs.trigger")
    path = f"/api/v1/internal/projects/{uuid.uuid4()}/run"
    first = await call(api, "POST", path, t)
    assert authorised(first)                                         # 404: no such project, but authenticated
    second = await call(api, "POST", path, t)
    assert second.status_code == 401 and code(second) == "TOKEN_REPLAYED"


async def test_a_replayed_token_cannot_trigger_after_a_failed_first_attempt(api):
    """A first attempt that fails downstream (404) still consumed the token: no second try with it."""
    t = token("dbt.runs.trigger")
    path = f"/api/v1/internal/projects/{uuid.uuid4()}/run"
    assert (await call(api, "POST", path, t)).status_code == 404
    assert (await call(api, "POST", path, t)).status_code == 401


# ── settings ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("other", [{"internal_service_token": "same"}, {"service_jwt_secret": "same"}, {"delegation_jwt_secret": "same"}])
def test_the_scheduler_secret_must_differ_from_every_other_service_secret(other):
    with pytest.raises(ValueError, match="must differ"):
        Settings(scheduler_jwt_secret="same", **other)


# ── every internal route demands a scheduler scope (drift guard) ─────────
def _internal_routes():
    from app.api.projects import router
    for r in router.routes:
        if "/internal" in getattr(r, "path", ""):
            yield r


def test_the_drift_guard_actually_sees_the_internal_routes():
    assert len(list(_internal_routes())) == len(ROUTES)


@pytest.mark.parametrize("route", list(_internal_routes()), ids=lambda r: f"{min(r.methods)} {r.path}")
def test_every_internal_route_requires_one_scheduler_scope(route):
    scopes = {getattr(d.call, "required_scope", None) for d in route.dependant.dependencies} - {None}
    table = {(m, p.replace(PID, "{project_id}").replace(RID, "{run_id}")): s for m, p, s in ROUTES}
    assert scopes == {table[(min(route.methods), route.path)]}
