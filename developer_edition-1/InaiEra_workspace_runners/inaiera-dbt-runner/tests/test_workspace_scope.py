"""DBT-012: the dbt project registry is shared by the whole org; `workspace_id` is a grouping label Workspace vouches for.

Before, the runner stored whatever `workspace_id` the request said, so a same-org caller could register a project into
ANOTHER user's workspace group, and nothing said whether the registry was per-workspace or org-wide. Now:

  * the registry is org-shared (documented in the model, the API and docs): access is org + role capability, never workspace;
  * a body `workspace_id` is accepted only if the Workspace service vouched for it in the signed delegated token
    (`workspace_id` claim, which Workspace sets only after checking the caller owns that workspace);
  * external CI simply omits it.
"""
from __future__ import annotations

import time
import uuid

import httpx
import pytest
from jose import jwt

from tests.perms import ALL_JOB
from app.core.auth import Principal
from app.core.config import get_settings
from app.core.service_auth import SERVICE_AUDIENCE
from app.core.workspace_scope import authorize_workspace
from app.main import app

SECRET = "workspace-to-dbtrunner-delegation-secret"
MINE, THEIRS = uuid.uuid4(), uuid.uuid4()
GIT = "https://github.com/acme/{}.git"


@pytest.fixture(autouse=True)
def delegation(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "delegation_jwt_secret", SECRET)
    monkeypatch.setattr(s, "delegation_jwt_allowed_issuers", ["workspace"])
    monkeypatch.setattr(s, "control_plane_audience", SERVICE_AUDIENCE)


def delegated(*, scope="dbt.projects.write", **over) -> str:
    now = int(time.time())
    claims = {"iss": "workspace", "sub": "user-42", "act": {"sub": "workspace"}, "aud": SERVICE_AUDIENCE, "scope": scope,
              "org_id": get_settings().org_id, "permissions": ALL_JOB, "email": "u@example.com",
              "jti": uuid.uuid4().hex, "iat": now, "exp": now + 60}
    claims.update(over)
    return jwt.encode(claims, SECRET, algorithm="HS256")


@pytest.fixture
async def api(run_db):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _register(api, token, *, repo="a", workspace=None):
    body = {"git_url": GIT.format(repo), "git_ref": "main"}
    if workspace is not None:
        body["workspace_id"] = str(workspace)
    return await api.post("/api/v1/projects/register", headers=_h(token), json=body)


async def _projects(api, token, **params):
    r = await api.get("/api/v1/projects", headers=_h(token), params=params)
    assert r.status_code == 200, r.text
    return r.json()


# ---- the rule itself -------------------------------------------------------
def _principal(ws):
    return Principal(user_id="u", org_id="o", permissions=frozenset(), email=None, issued_at=0, expires_at=0, workspace_id=ws)


def test_nothing_requested_needs_no_claim():
    assert authorize_workspace(_principal(None), None) is None


def test_a_workspace_equal_to_the_claim_is_accepted_whatever_the_case():
    assert authorize_workspace(_principal(str(MINE).upper()), MINE) == MINE


@pytest.mark.parametrize("claim", [None, "", str(THEIRS), "not-a-uuid"])
def test_a_workspace_the_token_does_not_vouch_for_is_refused(claim):
    with pytest.raises(Exception) as e:
        authorize_workspace(_principal(claim), MINE)
    assert e.value.status_code == 403 and e.value.detail["code"] == "WORKSPACE_NOT_AUTHORIZED"


# ---- through the API -------------------------------------------------------
async def test_a_workspace_vouched_for_by_workspace_is_stored(api):
    r = await _register(api, delegated(workspace_id=str(MINE)), workspace=MINE)
    assert r.status_code == 201, r.text
    assert r.json()["workspace_id"] == str(MINE)


async def test_a_workspace_in_the_body_with_no_claim_is_refused_and_nothing_is_registered(api):
    r = await _register(api, delegated(), workspace=THEIRS)            # the pre-fix hole: any UUID was accepted
    assert r.status_code == 403 and r.json()["detail"]["code"] == "WORKSPACE_NOT_AUTHORIZED"
    assert await _projects(api, delegated(scope="dbt.projects.read")) == []


async def test_a_body_workspace_different_from_the_claim_is_refused(api):
    r = await _register(api, delegated(workspace_id=str(MINE)), workspace=THEIRS)
    assert r.status_code == 403
    assert await _projects(api, delegated(scope="dbt.projects.read")) == []


async def test_a_claim_alone_does_not_assign_a_workspace(api):
    r = await _register(api, delegated(workspace_id=str(MINE)))        # body has no workspace
    assert r.status_code == 201 and r.json()["workspace_id"] is None


async def test_external_ci_registers_without_a_workspace_but_cannot_assert_one(api, auth_headers):
    ok = await api.post("/api/v1/projects/register", headers=auth_headers, json={"git_url": GIT.format("ci"), "git_ref": "main"})
    assert ok.status_code == 201 and ok.json()["workspace_id"] is None
    bad = await api.post("/api/v1/projects/register", headers=auth_headers,
                         json={"git_url": GIT.format("ci2"), "git_ref": "main", "workspace_id": str(THEIRS)})
    assert bad.status_code == 403


async def test_the_workspace_cannot_be_changed_by_an_update(api):
    r = await _register(api, delegated(workspace_id=str(MINE)), workspace=MINE)
    pid = r.json()["id"]
    up = await api.patch(f"/api/v1/projects/{pid}", headers=_h(delegated(scope="dbt.projects.write")),
                         json={"workspace_id": str(THEIRS), "schedule_enabled": False})
    assert up.status_code == 200 and up.json()["workspace_id"] == str(MINE)


# ---- the model: the registry is org-shared ---------------------------------
async def test_the_registry_is_org_shared_workspace_is_only_a_grouping_filter(api):
    await _register(api, delegated(workspace_id=str(MINE)), repo="mine", workspace=MINE)
    await _register(api, delegated(workspace_id=str(THEIRS)), repo="theirs", workspace=THEIRS)
    read = delegated(scope="dbt.projects.read", workspace_id=str(MINE))
    assert {p["git_url"] for p in await _projects(api, read)} == {GIT.format("mine"), GIT.format("theirs")}   # org-wide
    assert {p["git_url"] for p in await _projects(api, read, workspace_id=str(THEIRS))} == {GIT.format("theirs")}   # a filter
    assert {p["git_url"] for p in await _projects(api, read, workspace_id=str(MINE))} == {GIT.format("mine")}


def test_the_org_shared_model_is_stated_where_people_read_it():
    from app.models import Project
    from app.schemas.project import ProjectRegister
    assert "org" in Project.__doc__.lower() and "NOT an ownership" in Project.__doc__
    assert ProjectRegister.model_fields["workspace_id"].default is None


# ---- X-005: a route added later cannot forget the rule -----------------------
def _body_models_with_workspace(handler):
    import typing
    # get_type_hints: the modules use `from __future__ import annotations`, so raw annotations are strings
    # (parameters only: a RESPONSE model naming workspace_id is just output)
    return [m.__name__ for name, m in typing.get_type_hints(handler).items()
            if name != "return" and hasattr(m, "model_fields") and "workspace_id" in m.model_fields]


def test_every_route_with_a_workspace_in_its_body_authorizes_it():
    import inspect

    from app.api.projects import router
    guarded = []
    for route in router.routes:
        models = _body_models_with_workspace(route.endpoint)
        if models:
            guarded.append(route.endpoint.__name__)
            assert "authorize_workspace(" in inspect.getsource(route.endpoint), (
                f"{route.endpoint.__name__} accepts {models} but never calls authorize_workspace: a body workspace_id "
                "would be trusted as request JSON (X-005 / DBT-012)")
    assert guarded == ["register_project"]          # the guard really sees the route it protects
