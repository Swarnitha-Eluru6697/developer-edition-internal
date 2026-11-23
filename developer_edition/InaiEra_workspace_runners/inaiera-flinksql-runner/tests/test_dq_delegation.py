"""X-001: the Data Quality service acts on a user's behalf here with a delegation key of its OWN.

DQ used to forward the user's own bearer (minted for Workspace) to this runner to read a job and to create or update its
quality job; once the runner stopped accepting Workspace-audience tokens those DQ calls failed with 401. Now DQ mints a
short-lived delegated token (iss and act.sub "dataquality", aud "flinksql-runner", ONE scope, sub = the user) signed with
DQ_DELEGATION_JWT_SECRET. DQ is an accepted issuer only while that key is set, and only for the scopes
DQ_DELEGATION_ALLOWED_SCOPES (read and edit a job): never deploy, stop or delete. Workspace's key cannot sign as DQ, DQ's
cannot sign as Workspace, and the permission check still applies on top using the delegated user's permissions.
"""
from __future__ import annotations

import asyncio
import time
import uuid

import pytest
from jose import jwt

from tests.perms import DQ_PUBLISHER, EDITOR, ENGINEER, VIEWER
from app.core import auth as core_auth
from app.core.config import Settings, get_settings
from app.core.service_auth import SERVICE_AUDIENCE

WORKSPACE_SECRET = "workspace-to-flink-delegation-secret"
DQ_SECRET = "dq-to-flink-delegation-secret-different"
JOB = str(uuid.uuid4())


@pytest.fixture(autouse=True)
def keys(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "delegation_jwt_secret", WORKSPACE_SECRET)
    monkeypatch.setattr(s, "delegation_jwt_allowed_issuers", ["workspace"])
    monkeypatch.setattr(s, "dq_delegation_jwt_secret", DQ_SECRET)
    return s


def from_dq(*, scope="flink.jobs.read", secret=DQ_SECRET, permissions=tuple(ENGINEER), **over) -> str:
    now = int(time.time())
    claims = {"iss": "dataquality", "act": {"sub": "dataquality"}, "sub": "user-42", "aud": SERVICE_AUDIENCE,
              "scope": scope, "org_id": get_settings().org_id, "permissions": list(permissions), "email": "u@example.com",
              "jti": uuid.uuid4().hex, "iat": now, "exp": now + 60}
    claims.update(over)
    return jwt.encode(claims, secret, algorithm="HS256")


def from_workspace(*, scope="flink.jobs.read", secret=WORKSPACE_SECRET, **over) -> str:
    return from_dq(scope=scope, secret=secret, iss="workspace", act={"sub": "workspace"}, **over)


def _get(client, token, path="/api/v1/jobs"):
    return client.get(path, headers={"Authorization": f"Bearer {token}"})


def _authorised(resp) -> bool:
    return resp.status_code not in (401, 403)


# ── what DQ may do ───────────────────────────────────────────────────────
def test_dq_can_read_a_job_as_the_user(client):
    assert _authorised(_get(client, from_dq(scope="flink.jobs.read"), f"/api/v1/jobs/{JOB}"))


def test_dq_can_create_a_quality_job_as_the_user(client):
    r = client.post("/api/v1/jobs", json={"name": "dq-q", "sql": "INSERT INTO t SELECT 1", "job_kind": "quality"},
                    headers={"Authorization": f"Bearer {from_dq(scope='flink.jobs.edit')}"})
    assert _authorised(r), r.text


def test_the_principal_is_the_end_user_acting_through_dq():
    p = asyncio.run(core_auth.validate_delegated_jwt(from_dq(sub="alice", permissions=VIEWER)))
    assert (p.user_id, p.permissions, p.actor, p.scopes) == ("alice", frozenset(VIEWER), "dataquality", ["flink.jobs.read"])


@pytest.mark.parametrize("scope", ["flink.jobs.deploy", "flink.catalog.read", "flink.jobs.read flink.jobs.deploy"])
def test_dq_cannot_act_with_a_scope_outside_its_allow_list(client, scope):
    r = client.post(f"/api/v1/jobs/{JOB}/deploy", json={}, headers={"Authorization": f"Bearer {from_dq(scope=scope)}"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "INSUFFICIENT_SCOPE"


def test_the_users_role_still_limits_what_dq_can_do_for_them(client):
    """The edit scope allows the call; an analyst's ROLE does not."""
    r = client.post("/api/v1/jobs", json={"name": "dq-q", "sql": "INSERT INTO t SELECT 1"},
                    headers={"Authorization": f"Bearer {from_dq(scope='flink.jobs.edit', permissions=VIEWER)}"})
    assert r.status_code == 403


def test_a_read_token_from_dq_cannot_edit(client):
    r = client.post("/api/v1/jobs", json={"name": "dq-q", "sql": "INSERT INTO t SELECT 1"},
                    headers={"Authorization": f"Bearer {from_dq(scope='flink.jobs.read')}"})
    assert r.status_code == 403


# ── each issuer has its own key ──────────────────────────────────────────
def test_workspaces_key_cannot_sign_as_dq(client):
    assert _get(client, from_dq(secret=WORKSPACE_SECRET)).status_code == 401


def test_dqs_key_cannot_sign_as_workspace(client):
    assert _get(client, from_workspace(secret=DQ_SECRET)).status_code == 401


def test_workspace_tokens_still_work(client):
    assert _authorised(_get(client, from_workspace()))


def test_dq_is_not_an_accepted_issuer_until_its_key_is_configured(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "dq_delegation_jwt_secret", None)
    r = _get(client, from_dq())
    assert r.status_code == 401 and r.json()["detail"]["code"] == "DELEGATION_NOT_CONFIGURED"
    assert _authorised(_get(client, from_workspace()))                 # Workspace is unaffected


@pytest.mark.parametrize("make", [
    lambda: from_dq(act={"sub": "workspace"}),                         # actor is not the issuer
    lambda: from_dq(aud="dbtrunner"),
    lambda: from_dq(exp=int(time.time()) + 3600),                      # long-lived
    lambda: from_dq(exp=int(time.time()) - 120, iat=int(time.time()) - 180),
    lambda: from_dq(org_id="some-other-org"),
], ids=["wrong-actor", "wrong-audience", "long-lived", "expired", "other-org"])
def test_an_invalid_dq_token_is_refused(client, make):
    assert _get(client, make()).status_code in (401, 403)


def test_a_service_secret_cannot_sign_a_dq_delegation(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "internal_service_token", "the-static-service-token")
    assert _get(client, from_dq(secret="the-static-service-token")).status_code == 401


# ── configuration ────────────────────────────────────────────────────────
def test_the_dq_key_must_differ_from_every_other_key():
    with pytest.raises(ValueError, match="DQ_DELEGATION_JWT_SECRET must differ"):
        Settings(dq_delegation_jwt_secret="same", delegation_jwt_secret="same")
    with pytest.raises(ValueError, match="DQ_DELEGATION_JWT_SECRET must differ"):
        Settings(dq_delegation_jwt_secret="same", internal_service_token="same")


def test_dq_is_off_by_default_and_limited_to_read_and_edit():
    s = Settings()
    assert s.dq_delegation_jwt_secret is None and s.dq_delegation_issuer == "dataquality"
    assert s.dq_delegation_allowed_scopes == ["flink.jobs.read", "flink.jobs.edit"]


# ── W-009: who may have DQ create their quality job ──────────────────────────────────────────────
def _create_quality_job(client, token):
    return client.post("/api/v1/jobs", json={"name": "dq-q", "sql": "INSERT INTO t SELECT 1", "job_kind": "quality"},
                       headers={"Authorization": f"Bearer {token}"})


@pytest.mark.parametrize("held", [ENGINEER, DQ_PUBLISHER, ["workspace:dq:publish"]], ids=str)
def test_a_dq_publisher_can_have_dq_create_their_quality_job(client, held):
    """workspace:dq:publish (DQ's apply) is the right to deliver a quality job; one may hold it without any Flink permission."""
    r = _create_quality_job(client, from_dq(scope="flink.jobs.edit", permissions=held))
    assert _authorised(r), r.text


@pytest.mark.parametrize("held", [[], VIEWER, EDITOR, ["workspace:dq:edit", "workspace:dq:run"]], ids=str)
def test_a_caller_without_dq_publish_cannot_have_dq_create_their_quality_job(client, held):
    r = _create_quality_job(client, from_dq(scope="flink.jobs.edit", permissions=held))
    assert r.status_code == 403 and r.json()["detail"]["code"] == "FORBIDDEN"


def test_the_steward_exception_exists_only_for_tokens_issued_by_dq(client):
    """The same DQ publisher calling through Workspace (or any non-DQ issuer) has no Flink edit permission."""
    r = _create_quality_job(client, from_workspace(scope="flink.jobs.edit", permissions=DQ_PUBLISHER))
    assert r.status_code == 403 and r.json()["detail"]["code"] == "FORBIDDEN"


def test_dq_issued_tokens_do_not_widen_the_deploy_capability(client):
    r = client.post(f"/api/v1/jobs/{JOB}/deploy", json={}, headers={"Authorization": f"Bearer {from_dq(scope='flink.jobs.deploy', permissions=DQ_PUBLISHER)}"})
    assert r.status_code == 403


def test_dq_publish_alone_grants_no_flink_permission_outside_dq_calls():
    from app.core import deps
    p = type("P", (), {"permissions": frozenset(DQ_PUBLISHER), "actor": None})()
    assert deps.effective_permissions_needed(p, ("workspace:flink:edit",)) == ("workspace:flink:edit",)
    assert "workspace:flink:edit" not in p.permissions
