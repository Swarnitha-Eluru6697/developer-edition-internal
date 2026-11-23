"""X-001: Workspace -> dbt Runner uses a short-lived DELEGATED token, not the user's bearer.

Target pattern:  Workspace --(HS256, aud=dbtrunner, one scope, act=workspace, <=300s)--> dbt Runner
External CI   :  a control-plane service-principal token with aud=dbtrunner and the route's scope.
A token minted for Workspace (aud=workspace.inaiera) must mean nothing here, and a token without the
route's scope is refused, so no credential is meaningful to more than one trust domain.
"""
from __future__ import annotations

import time
import uuid

import httpx
import pytest
from jose import jwt

from tests.perms import ALL_JOB
from app.core.config import Settings, get_settings
from app.core.service_auth import SERVICE_AUDIENCE
from app.main import app

SECRET = "workspace-to-dbtrunner-delegation-secret"
ROUTE_SCOPES = {
    "dbt.projects.read", "dbt.projects.write", "dbt.runs.read", "dbt.runs.trigger",
}


@pytest.fixture(autouse=True)
def delegation(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "delegation_jwt_secret", SECRET)
    monkeypatch.setattr(s, "delegation_jwt_allowed_issuers", ["workspace"])
    monkeypatch.setattr(s, "control_plane_audience", SERVICE_AUDIENCE)
    return s


def delegated(*, scope="dbt.projects.read", secret=SECRET, drop=(), **over) -> str:
    now = int(time.time())
    claims = {
        "iss": "workspace", "sub": "user-42", "act": {"sub": "workspace"}, "aud": SERVICE_AUDIENCE,
        "scope": scope, "org_id": get_settings().org_id, "permissions": ALL_JOB, "email": "u@example.com",
        "jti": uuid.uuid4().hex, "iat": now, "exp": now + 60,
    }
    claims.update(over)
    for k in drop:
        claims.pop(k, None)
    return jwt.encode(claims, secret, algorithm="HS256")


@pytest.fixture
async def api(run_db):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _list(api, token):
    return await api.get("/api/v1/projects", headers=_bearer(token))


# ── accepted ─────────────────────────────────────────────────────────────
async def test_a_delegated_token_with_the_route_scope_is_accepted(api):
    assert (await _list(api, delegated(scope="dbt.projects.read"))).status_code == 200


async def test_a_read_token_can_be_presented_again(api):
    t = delegated(scope="dbt.projects.read")
    assert (await _list(api, t)).status_code == 200
    assert (await _list(api, t)).status_code == 200            # polling reads must not need a new token each time


async def test_the_delegated_user_is_the_principal_not_workspace(api, run_db):
    """The runner must act as the END USER (sub / permissions), with Workspace only as the actor; role names are ignored."""
    from app.core.auth import validate_delegated_jwt
    p = await validate_delegated_jwt(delegated(sub="alice", permissions=["workspace:dbt:run"], roles=["workspace_admin"]))
    assert (p.user_id, p.permissions, p.actor) == ("alice", frozenset({"workspace:dbt:run"}), "workspace")
    assert not hasattr(p, "roles")


# ── scope is enforced ────────────────────────────────────────────────────
async def test_a_token_without_the_routes_scope_is_forbidden(api):
    r = await _list(api, delegated(scope="dbt.runs.trigger"))
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "INSUFFICIENT_SCOPE"


async def test_a_token_with_no_scope_at_all_is_forbidden(api):
    assert (await _list(api, delegated(scope=""))).status_code in (401, 403)


async def test_a_trigger_token_cannot_trigger_a_different_scopes_route(api):
    r = await api.post("/api/v1/projects/register", headers=_bearer(delegated(scope="dbt.runs.trigger")), json={})
    assert r.status_code in (403, 422) and r.status_code != 201


# ── audience / trust-domain separation ───────────────────────────────────
async def test_a_workspace_audience_user_token_is_no_longer_accepted(api, mint_jwt):
    """THE X-001 reproduction: the dev/browser token (aud workspace.inaiera) used to return 200 here."""
    r = await _list(api, mint_jwt(audience="workspace.inaiera"))
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "INVALID_TOKEN"


async def test_a_token_minted_for_the_flink_runner_is_rejected(api):
    assert (await _list(api, delegated(aud="flinksql-runner"))).status_code == 401


async def test_external_ci_service_principal_with_audience_and_scope_is_accepted(api, mint_jwt):
    assert (await _list(api, mint_jwt(audience="dbtrunner", scopes=["dbt.projects.read"], permissions=ALL_JOB))).status_code == 200


async def test_external_ci_token_without_the_scope_is_forbidden(api, mint_jwt):
    r = await _list(api, mint_jwt(audience="dbtrunner", scopes=["dbt.runs.trigger"]))
    assert r.status_code == 403


async def test_external_ci_token_without_any_scope_is_forbidden(api, mint_jwt):
    assert (await _list(api, mint_jwt(audience="dbtrunner"))).status_code == 403


# ── invalid delegated tokens ─────────────────────────────────────────────
@pytest.mark.parametrize("make", [
    lambda: "not-a-jwt",
    lambda: delegated(secret="some-other-secret"),                       # not signed by Workspace
    lambda: delegated(iss="dataquality", act={"sub": "dataquality"}),   # self-consistent, but not an ALLOWED issuer
    lambda: delegated(iss="dataquality"),                                # issuer and actor disagree                                # not an allowed issuer
    lambda: delegated(aud="somebody-else"),
    lambda: delegated(exp=int(time.time()) - 120, iat=int(time.time()) - 180),   # expired (beyond the clock-skew allowance)
    lambda: delegated(iat=int(time.time()) + 600, exp=int(time.time()) + 660),  # issued in the future
    lambda: delegated(exp=int(time.time()) + 3600),                      # lifetime far beyond the cap
    lambda: delegated(drop=("jti",)),
    lambda: delegated(drop=("sub",)),
    lambda: delegated(drop=("org_id",)),
    lambda: delegated(drop=("exp",)),
    lambda: delegated(act={"sub": "someone-else"}),                      # actor is not Workspace
], ids=["malformed", "wrong-key", "issuer-not-allowed", "issuer-actor-mismatch", "wrong-audience", "expired", "future-iat",
        "long-lived", "no-jti", "no-sub", "no-org", "no-exp", "wrong-actor"])
async def test_an_invalid_delegated_token_is_rejected(api, make):
    r = await _list(api, make())
    assert r.status_code == 401, r.text


async def test_a_delegated_token_for_another_org_is_forbidden(api):
    r = await _list(api, delegated(org_id="some-other-org"))
    assert r.status_code == 403 and r.json()["detail"]["code"] == "ORG_MISMATCH"


async def test_a_token_signed_with_the_dq_service_secret_is_not_a_delegation(api, monkeypatch):
    """Delegation has its OWN key: DQ's SERVICE_JWT_SECRET holder cannot impersonate a user."""
    s = get_settings()
    monkeypatch.setattr(s, "service_jwt_secret", "dq-secret")
    assert (await _list(api, delegated(secret="dq-secret"))).status_code == 401


async def test_without_a_configured_delegation_secret_delegated_tokens_are_refused(api, monkeypatch):
    monkeypatch.setattr(get_settings(), "delegation_jwt_secret", None)
    r = await _list(api, delegated())
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "DELEGATION_NOT_CONFIGURED"


async def test_the_raw_internal_service_token_is_not_a_delegation(api, monkeypatch):
    monkeypatch.setattr(get_settings(), "internal_service_token", "raw-internal")
    assert (await _list(api, "raw-internal")).status_code == 401


async def test_hs256_signed_with_the_jwks_public_key_is_not_a_ci_token(api, mint_jwt):
    """alg confusion: python-jose refuses to build this, so forge it by hand, as an attacker would."""
    import base64
    import hashlib
    import hmac
    import json

    from jose import jwk as _jwk

    from app.core.auth import _jwks_cache
    pub = next(iter(_jwks_cache._keys.values()))
    pem = _jwk.construct(pub).to_pem()
    now = int(time.time())
    b64 = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=")
    signing_input = b64({"alg": "HS256", "typ": "JWT", "kid": pub["kid"]}) + b"." + b64({
        "iss": get_settings().control_plane_issuer, "aud": "dbtrunner", "sub": "x", "org_id": get_settings().org_id,
        "scope": "dbt.projects.read", "jti": uuid.uuid4().hex, "iat": now, "exp": now + 60})
    sig = base64.urlsafe_b64encode(hmac.new(pem, signing_input, hashlib.sha256).digest()).rstrip(b"=")
    assert (await _list(api, (signing_input + b"." + sig).decode())).status_code == 401


# ── replay: state-changing delegations are single use ────────────────────
async def test_a_trigger_token_works_once_then_is_a_replay(api, run_db):
    t = delegated(scope="dbt.runs.trigger")
    pid = uuid.uuid4()
    first = await api.post(f"/api/v1/projects/{pid}/run", headers=_bearer(t), json={})
    assert first.status_code != 401, first.text                 # authenticated (404: no such project)
    second = await api.post(f"/api/v1/projects/{pid}/run", headers=_bearer(t), json={})
    assert second.status_code == 401
    assert second.json()["detail"]["code"] == "TOKEN_REPLAYED"


# ── every user-facing route demands a scope (drift guard) ────────────────
def _user_routes():
    # Walk the router itself: `app.routes` wraps included routers lazily in newer FastAPI and would list none.
    from app.api.projects import router
    for r in router.routes:
        path = getattr(r, "path", "")
        if "/internal" not in path and not path.endswith("/schema-tests") and path != "/api/v1/validate":
            yield r


def test_the_drift_guard_actually_sees_the_user_routes():
    """A guard that collects nothing passes vacuously (pytest skips an empty parameter set)."""
    assert len(list(_user_routes())) == 12


@pytest.mark.parametrize("route", list(_user_routes()), ids=lambda r: f"{min(r.methods)} {r.path}")
def test_every_user_route_requires_a_scope(route):
    names = {getattr(d.call, "required_scope", None) for d in route.dependant.dependencies}
    names.discard(None)
    assert len(names) == 1, f"{route.path} does not require exactly one scope: {names}"
    assert names <= ROUTE_SCOPES


def test_settings_refuse_a_delegation_secret_equal_to_another_service_secret():
    with pytest.raises(ValueError, match="must differ"):
        Settings(delegation_jwt_secret="same", service_jwt_secret="same")
    with pytest.raises(ValueError, match="must differ"):
        Settings(delegation_jwt_secret="same", internal_service_token="same")


def test_the_runners_default_audience_is_its_own_not_workspaces(monkeypatch):
    """The root of X-001: the default used to be "workspace.inaiera", Workspace's own audience."""
    monkeypatch.delenv("CONTROL_PLANE_AUDIENCE", raising=False)
    assert Settings().control_plane_audience == "dbtrunner"


# ── DBT-002: own audience only; externally issued tokens are short-lived ──────────────────────
@pytest.mark.parametrize("aud", ["workspace.inaiera", "flinksql-runner", "dq", "", "  "])
def test_settings_refuse_another_services_audience(aud):
    with pytest.raises(ValueError, match="CONTROL_PLANE_AUDIENCE"):
        Settings(control_plane_audience=aud)


def test_settings_accept_the_runners_own_audience():
    assert Settings(control_plane_audience="dbtrunner").control_plane_audience == "dbtrunner"


async def test_a_long_lived_external_token_is_refused(api, mint_jwt):
    """A browser-session-length token is not a service principal's: refused even though signature, issuer, audience and scope are fine."""
    t = mint_jwt(audience="dbtrunner", scopes=["dbt.projects.read"], permissions=ALL_JOB, expires_in=8 * 3600)
    r = await _list(api, t)
    assert r.status_code == 401 and r.json()["detail"]["code"] == "TOKEN_LIFETIME_TOO_LONG"


async def test_an_external_token_at_the_lifetime_cap_is_accepted(api, mint_jwt):
    cap = get_settings().external_jwt_max_lifetime_seconds
    t = mint_jwt(audience="dbtrunner", scopes=["dbt.projects.read"], permissions=ALL_JOB, expires_in=cap)
    assert (await _list(api, t)).status_code == 200
