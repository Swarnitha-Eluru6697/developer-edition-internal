"""X-001: Workspace -> FlinkSQL Runner uses a short-lived DELEGATED token, not the user's bearer.

Workspace --(HS256, aud=flinksql-runner, ONE scope, sub=user, act=workspace, short-lived)--> runner.
External CI: a control-plane service-principal token with aud=flinksql-runner and the route's scope.
A token minted for Workspace (aud=workspace.inaiera) means nothing here; a token without the route's scope
is refused; the permission check still applies on top, using the delegated user's permissions.
"""
from __future__ import annotations

import time
import uuid

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwk, jwt

from tests.perms import DQ_PUBLISHER, EDITOR, ENGINEER, VIEWER
from app.core import auth as core_auth
from app.core.config import Settings, get_settings
from app.core.service_auth import SERVICE_AUDIENCE

SECRET = "workspace-to-flink-delegation-secret"
ISSUER = "http://test-issuer"
KID = "ci-key-1"
JOB = str(uuid.uuid4())
ROUTE_SCOPES = {"flink.jobs.read", "flink.jobs.edit", "flink.jobs.deploy", "flink.catalog.read"}


def _rsa():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()
    pub = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    j = jwk.construct(pub, "RS256").to_dict()
    j.update(kid=KID, alg="RS256", use="sig")
    return priv, {"keys": [j]}


CI_PRIVATE, CI_JWKS = _rsa()


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "delegation_jwt_secret", SECRET)
    monkeypatch.setattr(s, "delegation_jwt_allowed_issuers", ["workspace"])
    monkeypatch.setattr(s, "control_plane_audience", SERVICE_AUDIENCE)
    monkeypatch.setattr(s, "control_plane_issuer", ISSUER)

    async def fake_jwks(*, force_refresh=False):
        return CI_JWKS, False
    monkeypatch.setattr(core_auth, "get_jwks", fake_jwks)
    return s


def delegated(*, scope="flink.jobs.read", secret=SECRET, drop=(), **over) -> str:
    now = int(time.time())
    claims = {"iss": "workspace", "sub": "user-42", "act": {"sub": "workspace"}, "aud": SERVICE_AUDIENCE,
              "scope": scope, "org_id": get_settings().org_id, "permissions": ENGINEER, "email": "u@example.com",
              "jti": uuid.uuid4().hex, "iat": now, "exp": now + 60}
    claims.update(over)
    for k in drop:
        claims.pop(k, None)
    return jwt.encode(claims, secret, algorithm="HS256")


def ci_token(*, aud=SERVICE_AUDIENCE, scope=None, permissions=tuple(ENGINEER), **over) -> str:
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": aud, "sub": "ci-bot", "org_id": get_settings().org_id, "permissions": list(permissions),
              "iat": now, "exp": now + 600}
    if scope is not None:
        claims["scope"] = scope
    claims.update(over)
    return jwt.encode(claims, CI_PRIVATE, algorithm="RS256", headers={"kid": KID})


def _get(client, token, path="/api/v1/jobs"):
    return client.get(path, headers={"Authorization": f"Bearer {token}"})


def _authorised(resp) -> bool:
    """Authentication and authorisation passed (what follows is the DB, not under test here)."""
    return resp.status_code not in (401, 403)


# ── accepted ─────────────────────────────────────────────────────────────
def test_a_delegated_token_with_the_route_scope_is_accepted(client):
    assert _authorised(_get(client, delegated(scope="flink.jobs.read")))


def test_the_delegated_user_is_the_principal_not_workspace():
    import asyncio
    p = asyncio.run(core_auth.validate_delegated_jwt(delegated(sub="alice", permissions=VIEWER, roles=["workspace_admin"])))
    assert (p.user_id, p.permissions, p.actor, p.scopes) == ("alice", frozenset(VIEWER), "workspace", ["flink.jobs.read"])
    assert not hasattr(p, "roles")


# ── scope is enforced, and RBAC still sits on top ────────────────────────
def test_a_token_without_the_routes_scope_is_forbidden(client):
    r = _get(client, delegated(scope="flink.jobs.deploy"))
    assert r.status_code == 403 and r.json()["detail"]["code"] == "INSUFFICIENT_SCOPE"


def test_a_deploy_scope_does_not_let_an_analyst_deploy(client):
    """Scope says the call is allowed; the delegated user's ROLE must still allow it."""
    r = client.post(f"/api/v1/jobs/{JOB}/deploy", json={},
                    headers={"Authorization": f"Bearer {delegated(scope='flink.jobs.deploy', permissions=EDITOR)}"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "FORBIDDEN"


def test_a_deploy_scope_with_a_deploy_role_passes_authorisation(client):
    r = client.post(f"/api/v1/jobs/{JOB}/deploy", json={},
                    headers={"Authorization": f"Bearer {delegated(scope='flink.jobs.deploy', permissions=ENGINEER)}"})
    assert _authorised(r)


def test_a_read_token_cannot_edit(client):
    r = client.post("/api/v1/jobs", json={"name": "j", "sql": "INSERT INTO t SELECT 1"},
                    headers={"Authorization": f"Bearer {delegated(scope='flink.jobs.read')}"})
    assert r.status_code == 403


# ── trust-domain separation ──────────────────────────────────────────────
def test_a_workspace_audience_user_token_is_no_longer_accepted(client):
    """THE X-001 reproduction: a token minted for Workspace used to return 200 here."""
    r = _get(client, ci_token(aud="workspace.inaiera", scope="flink.jobs.read"))
    assert r.status_code == 401


def test_a_token_minted_for_the_dbt_runner_is_rejected(client):
    assert _get(client, delegated(aud="dbtrunner")).status_code == 401


def test_external_ci_with_audience_and_scope_is_accepted(client):
    assert _authorised(_get(client, ci_token(scope="flink.jobs.read")))


def test_external_ci_without_the_scope_is_forbidden(client):
    assert _get(client, ci_token(scope="flink.jobs.deploy")).status_code == 403


def test_external_ci_without_any_scope_is_forbidden(client):
    assert _get(client, ci_token()).status_code == 403


# ── invalid delegated tokens ─────────────────────────────────────────────
@pytest.mark.parametrize("make", [
    lambda: "not-a-jwt",
    lambda: delegated(secret="some-other-secret"),
    lambda: delegated(iss="dataquality", act={"sub": "dataquality"}),   # self-consistent, but not an ALLOWED issuer
    lambda: delegated(iss="dataquality"),                                # issuer and actor disagree
    lambda: delegated(aud="somebody-else"),
    lambda: delegated(exp=int(time.time()) - 120, iat=int(time.time()) - 180),
    lambda: delegated(iat=int(time.time()) + 600, exp=int(time.time()) + 660),
    lambda: delegated(exp=int(time.time()) + 3600),
    lambda: delegated(drop=("jti",)),
    lambda: delegated(drop=("sub",)),
    lambda: delegated(drop=("org_id",)),
    lambda: delegated(drop=("exp",)),
    lambda: delegated(act={"sub": "someone-else"}),
], ids=["malformed", "wrong-key", "issuer-not-allowed", "issuer-actor-mismatch", "wrong-audience", "expired", "future-iat",
        "long-lived", "no-jti", "no-sub", "no-org", "no-exp", "wrong-actor"])
def test_an_invalid_delegated_token_is_rejected(client, make):
    assert _get(client, make()).status_code == 401


def test_a_delegated_token_for_another_org_is_forbidden(client):
    r = _get(client, delegated(org_id="some-other-org"))
    assert r.status_code == 403 and r.json()["detail"]["code"] == "ORG_MISMATCH"


def test_the_internal_service_token_signing_key_is_not_a_delegation_key(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "internal_service_token", "internal-hmac")
    assert _get(client, delegated(secret="internal-hmac")).status_code == 401


def test_without_a_configured_secret_delegated_tokens_are_refused(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "delegation_jwt_secret", None)
    r = _get(client, delegated())
    assert r.status_code == 401 and r.json()["detail"]["code"] == "DELEGATION_NOT_CONFIGURED"


def test_a_missing_authorization_header_is_still_a_401(client):
    assert client.get("/api/v1/jobs").status_code == 401


# ── every user-facing route demands a scope (drift guard) ────────────────
def _user_routes():
    # Walk the routers themselves: `app.routes` wraps included routers lazily in newer FastAPI and would list none.
    from app.api.jobs import router as jobs
    from app.api.kafka_catalog import router as kafka
    yield from jobs.routes
    yield from kafka.routes


def test_the_drift_guard_actually_sees_the_user_routes():
    """A guard that collects nothing passes vacuously (pytest skips an empty parameter set)."""
    assert len(list(_user_routes())) >= 14


def _all_calls(dependant):
    for d in dependant.dependencies:
        yield d.call
        yield from _all_calls(d)


@pytest.mark.parametrize("route", list(_user_routes()), ids=lambda r: f"{min(r.methods)} {r.path}")
def test_every_user_route_requires_a_scope(route):
    scopes = {getattr(c, "required_scope", None) for c in _all_calls(route.dependant)} - {None}
    assert len(scopes) == 1, f"{route.path} does not require exactly one scope: {scopes}"
    assert scopes <= ROUTE_SCOPES


# ── settings ─────────────────────────────────────────────────────────────
def test_the_default_audience_is_the_runners_own_not_workspaces(monkeypatch):
    monkeypatch.delenv("CONTROL_PLANE_AUDIENCE", raising=False)
    assert Settings().control_plane_audience == "flinksql-runner"


def test_settings_refuse_a_delegation_secret_equal_to_the_internal_token():
    with pytest.raises(ValueError, match="must differ"):
        Settings(delegation_jwt_secret="same", internal_service_token="same")
