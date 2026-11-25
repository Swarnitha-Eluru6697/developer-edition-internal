"""X-001: the Data Quality service acts on a user's behalf here with a delegation key of its OWN.

DQ used to forward the user's own bearer (minted for Workspace) to this runner to look a project up when a rule is bound
to it; once the runner stopped accepting Workspace-audience tokens that DQ call failed with 401. Now DQ mints a
short-lived delegated token (iss and act.sub "dataquality", aud "dbtrunner", scope dbt.projects.read, sub = the user)
signed with DQ_DELEGATION_JWT_SECRET. DQ is an accepted issuer only while that key is set, and only for the scopes in
DQ_DELEGATION_ALLOWED_SCOPES (read a project): it cannot start runs or register/edit projects. Workspace's key cannot sign
as DQ, DQ's cannot sign as Workspace, and the permission check still applies using the delegated user's permissions.
"""
from __future__ import annotations

import time
import uuid

import httpx
import pytest
from jose import jwt

from app.core.auth import validate_delegated_jwt
from app.core.config import Settings, get_settings
from app.core.service_auth import SERVICE_AUDIENCE
from app.main import app
from tests.perms import ALL_JOB

WORKSPACE_SECRET = "workspace-to-dbtrunner-delegation-secret"
DQ_SECRET = "dq-to-dbtrunner-delegation-secret-different"


@pytest.fixture(autouse=True)
def keys(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "delegation_jwt_secret", WORKSPACE_SECRET)
    monkeypatch.setattr(s, "delegation_jwt_allowed_issuers", ["workspace"])
    monkeypatch.setattr(s, "dq_delegation_jwt_secret", DQ_SECRET)
    monkeypatch.setattr(s, "control_plane_audience", SERVICE_AUDIENCE)
    return s


def from_dq(*, scope="dbt.projects.read", secret=DQ_SECRET, permissions=tuple(ALL_JOB), **over) -> str:
    now = int(time.time())
    claims = {"iss": "dataquality", "act": {"sub": "dataquality"}, "sub": "user-42", "aud": SERVICE_AUDIENCE,
              "scope": scope, "org_id": get_settings().org_id, "permissions": list(permissions), "email": "u@example.com",
              "jti": uuid.uuid4().hex, "iat": now, "exp": now + 60}
    claims.update(over)
    return jwt.encode(claims, secret, algorithm="HS256")


def from_workspace(*, scope="dbt.projects.read", secret=WORKSPACE_SECRET, **over) -> str:
    return from_dq(scope=scope, secret=secret, iss="workspace", act={"sub": "workspace"}, **over)


@pytest.fixture
async def api(run_db):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _list(api, token):
    return await api.get("/api/v1/projects", headers={"Authorization": f"Bearer {token}"})


async def _post(api, token, path, body=None):
    return await api.post(f"/api/v1/projects{path}", json=body or {}, headers={"Authorization": f"Bearer {token}"})


# ── what DQ may do ───────────────────────────────────────────────────────
async def test_dq_can_read_projects_as_the_user(api):
    assert (await _list(api, from_dq())).status_code == 200


async def test_the_principal_is_the_end_user_acting_through_dq():
    p = await validate_delegated_jwt(from_dq(sub="alice", permissions=["workspace:dbt:edit"]))
    assert (p.user_id, p.permissions, p.actor, p.scopes) == ("alice", frozenset({"workspace:dbt:edit"}), "dataquality",
                                                             ["dbt.projects.read"])


@pytest.mark.parametrize("scope", ["dbt.runs.trigger", "dbt.projects.write", "dbt.runs.read",
                                   "dbt.projects.read dbt.runs.trigger"])
async def test_dq_cannot_act_with_a_scope_outside_its_allow_list(api, scope):
    r = await _post(api, from_dq(scope=scope), f"/{uuid.uuid4()}/run")
    assert r.status_code == 403 and r.json()["detail"]["code"] == "INSUFFICIENT_SCOPE"


async def test_dq_cannot_register_a_project(api):
    r = await _post(api, from_dq(scope="dbt.projects.write"), "/register", {"git_url": "https://github.com/a/b.git"})
    assert r.status_code == 403


async def test_a_dq_read_token_can_be_presented_again(api):
    t = from_dq()
    assert (await _list(api, t)).status_code == 200 and (await _list(api, t)).status_code == 200


# ── each issuer has its own key ──────────────────────────────────────────
async def test_workspaces_key_cannot_sign_as_dq(api):
    assert (await _list(api, from_dq(secret=WORKSPACE_SECRET))).status_code == 401


async def test_dqs_key_cannot_sign_as_workspace(api):
    assert (await _list(api, from_workspace(secret=DQ_SECRET))).status_code == 401


async def test_workspace_tokens_still_work(api):
    assert (await _list(api, from_workspace())).status_code == 200


async def test_dq_is_not_an_accepted_issuer_until_its_key_is_configured(api, monkeypatch):
    monkeypatch.setattr(get_settings(), "dq_delegation_jwt_secret", None)
    r = await _list(api, from_dq())
    assert r.status_code == 401 and r.json()["detail"]["code"] == "DELEGATION_NOT_CONFIGURED"
    assert (await _list(api, from_workspace())).status_code == 200


@pytest.mark.parametrize("make", [
    lambda: from_dq(act={"sub": "workspace"}),
    lambda: from_dq(aud="flinksql-runner"),
    lambda: from_dq(exp=int(time.time()) + 3600),
    lambda: from_dq(exp=int(time.time()) - 120, iat=int(time.time()) - 180),
    lambda: from_dq(org_id="some-other-org"),
], ids=["wrong-actor", "wrong-audience", "long-lived", "expired", "other-org"])
async def test_an_invalid_dq_token_is_refused(api, make):
    assert (await _list(api, make())).status_code in (401, 403)


async def test_the_dq_schema_tests_service_secret_cannot_sign_a_user(api, monkeypatch):
    """DQ also holds the runner's SERVICE_JWT_SECRET (schema-tests); that key must not mint a delegated user."""
    monkeypatch.setattr(get_settings(), "service_jwt_secret", "dq-schema-tests-service-secret")
    assert (await _list(api, from_dq(secret="dq-schema-tests-service-secret"))).status_code == 401


# ── configuration ────────────────────────────────────────────────────────
def test_the_dq_key_must_differ_from_every_other_key():
    for other in ("service_jwt_secret", "internal_service_token", "delegation_jwt_secret", "scheduler_jwt_secret"):
        with pytest.raises(ValueError, match="DQ_DELEGATION_JWT_SECRET must differ"):
            Settings(dq_delegation_jwt_secret="same", **{other: "same"})


def test_dq_is_off_by_default_and_limited_to_reading_projects():
    s = Settings()
    assert s.dq_delegation_jwt_secret is None and s.dq_delegation_issuer == "dataquality"
    assert s.dq_delegation_allowed_scopes == ["dbt.projects.read"]
