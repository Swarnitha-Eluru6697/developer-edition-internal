"""W-016 / FL-003 / DBT-012: a workspace association is an AUTHORIZED resource, not request JSON.

Before, the dbt and Flink proxies forwarded the `workspace_id` the browser sent without ever asking whether the caller owns
that workspace, and the runners stored it as metadata. Any same-org user with the dbt/Flink capability could therefore
attach a project or job to ANOTHER user's workspace. Now the proxy checks ownership first (a workspace that is not yours is
a 404, like every other workspace route) and carries the verified id in the signed delegated token.
"""
from __future__ import annotations

import asyncio
import json
import types
import uuid

import pytest
from jose import jwt

from app.api import dbt_proxy, flink_proxy
from app.core import service_tokens, workspace_scope
from app.core.errors import WorkspaceError

MINE, THEIRS = uuid.uuid4(), uuid.uuid4()
from app.core.auth import Principal
from role_seed import permissions_for

ME = Principal(user_id="u-me", org_id="org", roles=["data_engineer"], email="me@example.com", issued_at=0, expires_at=0,
               permissions=frozenset(permissions_for(["data_engineer"])))


class FakeWorkspaceService:
    """Two workspaces exist; MINE belongs to ME, THEIRS to someone else."""

    def __init__(self, db):
        pass

    async def ensure_user(self, principal):
        return types.SimpleNamespace(id="user-row-me")

    async def get(self, user, workspace_id):
        owner = {MINE: "user-row-me", THEIRS: "user-row-other"}.get(workspace_id)
        if owner != user.id:
            from app.core.errors import not_found
            raise not_found("workspace", str(workspace_id))
        return types.SimpleNamespace(id=workspace_id)


@pytest.fixture(autouse=True)
def fake_workspaces(monkeypatch):
    monkeypatch.setattr(workspace_scope, "WorkspaceService", FakeWorkspaceService)


def run(coro):
    return asyncio.run(coro)


# ---- the check itself ------------------------------------------------------
def test_nothing_requested_means_nothing_to_authorize():
    assert run(workspace_scope.authorize_workspace(None, ME, None)) is None


def test_my_own_workspace_is_authorized():
    assert run(workspace_scope.authorize_workspace(None, ME, MINE)) == MINE


@pytest.mark.parametrize("ws", [THEIRS, uuid.uuid4()])
def test_someone_elses_or_a_missing_workspace_is_a_404(ws):
    with pytest.raises(WorkspaceError) as e:
        run(workspace_scope.authorize_workspace(None, ME, ws))
    assert e.value.status_code == 404


@pytest.mark.parametrize("body,expected", [
    (b"", None), (b"not json", None), (b"[1]", None), (b"{}", None),
    (b'{"workspace_id": null}', None), (b'{"workspace_id": ""}', None),
    (json.dumps({"workspace_id": str(MINE)}).encode(), MINE),
])
def test_requested_workspace_id_is_read_from_a_json_body(body, expected):
    assert workspace_scope.requested_workspace_id(body) == expected


@pytest.mark.parametrize("bad", ["abc", "123", "not-a-uuid", 7])
def test_a_workspace_id_that_is_not_a_uuid_is_refused(bad):
    with pytest.raises(WorkspaceError) as e:
        workspace_scope.requested_workspace_id(json.dumps({"workspace_id": bad}).encode())
    assert e.value.status_code in (400, 422)


# ---- the signed claim ------------------------------------------------------
SECRET = "workspace-delegation-secret"


def _claims(token: str) -> dict:
    return jwt.decode(token, SECRET, algorithms=["HS256"], audience="dbtrunner")


def test_the_authorized_workspace_rides_in_the_signed_token():
    token = service_tokens.mint_delegated_token(secret=SECRET, audience="dbtrunner", scope="dbt.projects.write",
                                                principal=ME, workspace_id=MINE)
    assert _claims(token)["workspace_id"] == str(MINE)


def test_without_a_workspace_the_token_has_no_claim():
    token = service_tokens.mint_delegated_token(secret=SECRET, audience="dbtrunner", scope="dbt.projects.write", principal=ME)
    assert "workspace_id" not in _claims(token)


def test_delegated_authorization_passes_the_claim_through(monkeypatch):
    monkeypatch.setattr(service_tokens.get_settings(), "dbtrunner_delegation_jwt_secret", SECRET)
    header = service_tokens.delegated_authorization(ME, audience="dbtrunner", scope="dbt.projects.write", workspace_id=MINE)
    assert _claims(header.removeprefix("Bearer "))["workspace_id"] == str(MINE)


# ---- the routes ------------------------------------------------------------
class _Req:
    def __init__(self, body: dict | bytes):
        self._raw = body if isinstance(body, bytes) else json.dumps(body).encode()

    async def body(self):
        return self._raw


@pytest.fixture
def seen(monkeypatch):
    calls = {"proxy": [], "runner": [], "fetch": 0}

    async def fake_proxy(request, path, *rest, **kw):
        calls["proxy"].append((path, rest, kw))
        return "forwarded"

    async def fake_runner(user, scope, method, path, json_body=None, **kw):
        calls["runner"].append((json_body, kw))
        return types.SimpleNamespace(content=b"{}", status_code=201, headers={"content-type": "application/json"})

    async def fake_fetch(**kw):
        calls["fetch"] += 1
        return "INSERT INTO t SELECT 1;", "a.sql", "a" * 40, "main"

    monkeypatch.setattr(flink_proxy, "_proxy", fake_proxy)
    monkeypatch.setattr(dbt_proxy, "_proxy", fake_proxy)
    monkeypatch.setattr(flink_proxy, "_runner_call", fake_runner)
    monkeypatch.setattr(flink_proxy, "_fetch_sql_from_git", fake_fetch)
    monkeypatch.setattr(dbt_proxy, "_auth", lambda user, scope, workspace_id=None: f"AUTH[{scope}|{workspace_id}]")

    async def validate_forward(*a, **k):
        calls["validate"] = k.get("json")
        return types.SimpleNamespace(content=b"{}", status_code=200, headers={"content-type": "application/json"})
    monkeypatch.setattr(flink_proxy, "post_with_service_token", validate_forward)
    monkeypatch.setattr(flink_proxy.get_settings(), "flinksql_internal_token", "t")
    return calls


def test_a_flink_job_cannot_be_created_in_someone_elses_workspace(seen):
    with pytest.raises(WorkspaceError) as e:
        run(flink_proxy.create_job(_Req({"name": "j", "sql": "select 1", "workspace_id": str(THEIRS)}), ME, None))
    assert e.value.status_code == 404 and seen["proxy"] == []           # never reached the runner


def test_a_flink_job_in_my_own_workspace_is_forwarded_with_the_verified_id(seen):
    run(flink_proxy.create_job(_Req({"name": "j", "sql": "select 1", "workspace_id": str(MINE)}), ME, None))
    (_, _, kw), = seen["proxy"]
    assert kw["workspace_id"] == MINE


def test_a_flink_job_with_no_workspace_is_forwarded_with_no_claim(seen):
    run(flink_proxy.create_job(_Req({"name": "j", "sql": "select 1"}), ME, None))
    assert seen["proxy"][0][2]["workspace_id"] is None


def test_a_git_import_into_someone_elses_workspace_is_refused_before_any_clone(seen):
    payload = flink_proxy.FlinkJobFromGit(git_url="https://github.com/acme/jobs.git", workspace_id=THEIRS)
    with pytest.raises(WorkspaceError) as e:
        run(flink_proxy.create_job_from_git(payload, None, ME, None))
    assert e.value.status_code == 404 and seen["fetch"] == 0 and seen["runner"] == []


def test_a_git_import_into_my_workspace_carries_the_verified_id_in_the_body_and_the_token(seen):
    payload = flink_proxy.FlinkJobFromGit(git_url="https://github.com/acme/jobs.git", workspace_id=MINE)
    run(flink_proxy.create_job_from_git(payload, None, ME, None))
    body, kw = seen["runner"][0]
    assert body["workspace_id"] == str(MINE) and kw["workspace_id"] == MINE


def test_ad_hoc_validation_checks_the_workspace_too(seen):
    with pytest.raises(WorkspaceError):
        run(flink_proxy.validate_sql(flink_proxy.FlinkSqlValidate(sql="select 1", workspace_id=THEIRS), ME, None))
    assert "validate" not in seen
    run(flink_proxy.validate_sql(flink_proxy.FlinkSqlValidate(sql="select 1", workspace_id=MINE), ME, None))
    assert seen["validate"]["workspace_id"] == str(MINE)


def test_a_dbt_project_cannot_be_registered_in_someone_elses_workspace(seen):
    with pytest.raises(WorkspaceError) as e:
        run(dbt_proxy.register_project(_Req({"git_url": "https://h/o/r.git", "workspace_id": str(THEIRS)}), ME, None))
    assert e.value.status_code == 404 and seen["proxy"] == []


def test_a_dbt_project_in_my_workspace_is_forwarded_with_the_claim(seen):
    run(dbt_proxy.register_project(_Req({"git_url": "https://h/o/r.git", "workspace_id": str(MINE)}), ME, None))
    (_, rest, _), = seen["proxy"]
    assert rest[0] == f"AUTH[dbt.projects.write|{MINE}]"


def test_a_dbt_project_without_a_workspace_has_no_claim(seen):
    run(dbt_proxy.register_project(_Req({"git_url": "https://h/o/r.git"}), ME, None))
    assert seen["proxy"][0][1][0] == "AUTH[dbt.projects.write|None]"


def test_a_non_uuid_workspace_never_reaches_the_runner(seen):
    with pytest.raises(WorkspaceError):
        run(dbt_proxy.register_project(_Req({"git_url": "https://h/o/r.git", "workspace_id": "nope"}), ME, None))
    assert seen["proxy"] == []


def test_the_real_runner_call_puts_the_verified_workspace_in_the_token_it_sends(monkeypatch):
    """_runner_call is what from-git uses; the claim must reach the delegated token, not just the call's kwargs."""
    minted = []
    sent = {}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def request(self, *, method, url, json, headers):
            sent["headers"] = headers
            return types.SimpleNamespace(content=b"{}", status_code=200, headers={})

    monkeypatch.setattr(flink_proxy.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(flink_proxy, "delegated_authorization",
                        lambda user, *, audience, scope, workspace_id=None: minted.append(workspace_id) or "Bearer t")
    run(flink_proxy._runner_call(ME, "flink.jobs.edit", "POST", "jobs", {"name": "j"}, workspace_id=MINE))
    run(flink_proxy._runner_call(ME, "flink.jobs.read", "GET", "jobs/1"))
    assert minted == [MINE, None] and sent["headers"]["Authorization"] == "Bearer t"
