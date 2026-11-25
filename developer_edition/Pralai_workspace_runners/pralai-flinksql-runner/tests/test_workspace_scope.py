"""FL-003 / W-016: a Flink job's `workspace_id` is a grouping label Workspace vouches for, not request JSON.

Before, the runner stored whatever `workspace_id` the body said (it validated org + capability only), so a same-org user could
attach a job to another user's workspace. Now the registry is documented as org-shared (access = org + role capability) and a
body workspace is accepted only if it equals the `workspace_id` claim of the signed delegated token, which Workspace sets
only after checking that the caller owns that workspace.
"""
from __future__ import annotations

import time
import uuid

import httpx
import pytest
from jose import jwt

from tests.perms import DQ_PUBLISHER, EDITOR, ENGINEER, VIEWER
from app.core.auth import Principal
from app.core.config import get_settings
from app.core.service_auth import SERVICE_AUDIENCE
from app.core.workspace_scope import authorize_workspace
from app.main import app

SECRET = "workspace-to-flink-delegation-secret"
MINE, THEIRS = uuid.uuid4(), uuid.uuid4()
SQL = "INSERT INTO sink SELECT * FROM src;"


@pytest.fixture(autouse=True)
def delegation(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "delegation_jwt_secret", SECRET)
    monkeypatch.setattr(s, "delegation_jwt_allowed_issuers", ["workspace"])


def token(scope="flink.jobs.edit", **over) -> str:
    now = int(time.time())
    claims = {"iss": "workspace", "sub": "user-42", "act": {"sub": "workspace"}, "aud": SERVICE_AUDIENCE, "scope": scope,
              "org_id": get_settings().org_id, "permissions": ENGINEER, "email": "u@example.com",
              "jti": uuid.uuid4().hex, "iat": now, "exp": now + 60}
    claims.update(over)
    return jwt.encode(claims, SECRET, algorithm="HS256")


@pytest.fixture
async def api(pg_db):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _h(t: str) -> dict:
    return {"Authorization": f"Bearer {t}"}


async def _create(api, tok, *, name=None, workspace=None):
    body = {"name": name or f"j-{uuid.uuid4().hex[:6]}", "sql": SQL}
    if workspace is not None:
        body["workspace_id"] = str(workspace)
    return await api.post("/api/v1/jobs", headers=_h(tok), json=body)


async def _jobs(api, **params):
    r = await api.get("/api/v1/jobs", headers=_h(token("flink.jobs.read")), params=params)
    assert r.status_code == 200, r.text
    return r.json()


def _principal(ws):
    return Principal(user_id="u", org_id="o", workspace_id=ws)


def test_nothing_requested_needs_no_claim():
    assert authorize_workspace(_principal(None), None) is None


def test_a_workspace_equal_to_the_claim_is_accepted_whatever_the_case():
    assert authorize_workspace(_principal(str(MINE).upper()), MINE) == MINE


@pytest.mark.parametrize("claim", [None, "", str(THEIRS), "not-a-uuid"])
def test_a_workspace_the_token_does_not_vouch_for_is_refused(claim):
    with pytest.raises(Exception) as e:
        authorize_workspace(_principal(claim), MINE)
    assert e.value.status_code == 403 and e.value.detail["code"] == "WORKSPACE_NOT_AUTHORIZED"


async def test_a_workspace_vouched_for_by_workspace_is_stored(api):
    r = await _create(api, token(workspace_id=str(MINE)), workspace=MINE)
    assert r.status_code == 201, r.text
    assert r.json()["workspace_id"] == str(MINE)


async def test_a_workspace_in_the_body_with_no_claim_is_refused_and_nothing_is_created(api):
    r = await _create(api, token(), workspace=THEIRS)                  # the pre-fix hole
    assert r.status_code == 403 and r.json()["detail"]["code"] == "WORKSPACE_NOT_AUTHORIZED"
    assert await _jobs(api) == []


async def test_a_body_workspace_different_from_the_claim_is_refused(api):
    assert (await _create(api, token(workspace_id=str(MINE)), workspace=THEIRS)).status_code == 403
    assert await _jobs(api) == []


async def test_a_claim_alone_does_not_assign_a_workspace(api):
    r = await _create(api, token(workspace_id=str(MINE)))
    assert r.status_code == 201 and r.json()["workspace_id"] is None


async def test_the_registry_is_org_shared_workspace_is_only_a_grouping_filter(api):
    await _create(api, token(workspace_id=str(MINE)), name="mine", workspace=MINE)
    await _create(api, token(workspace_id=str(THEIRS)), name="theirs", workspace=THEIRS)
    assert {j["name"] for j in await _jobs(api)} == {"mine", "theirs"}                        # org-wide
    assert {j["name"] for j in await _jobs(api, workspace_id=str(THEIRS))} == {"theirs"}      # a filter, not isolation
    assert {j["name"] for j in await _jobs(api, workspace_id=str(MINE))} == {"mine"}


def test_the_org_shared_model_is_stated_where_people_read_it():
    from app.schemas import FlinkJobCreate
    assert FlinkJobCreate.model_fields["workspace_id"].default is None
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "app" / "core" / "workspace_scope.py").read_text()
    assert "org" in src and "NOT an ownership" in src


async def test_the_workspace_cannot_be_changed_by_an_update(api):
    r = await _create(api, token(workspace_id=str(MINE)), name="stays", workspace=MINE)
    up = await api.patch(f"/api/v1/jobs/{r.json()['id']}", headers=_h(token()), json={"workspace_id": str(THEIRS), "description": "x"})
    assert up.status_code == 200 and up.json()["workspace_id"] == str(MINE)


# ---- X-005: a route added later cannot forget the rule -----------------------
def test_every_route_with_a_workspace_in_its_body_authorizes_it():
    import inspect
    import typing

    from app.api import jobs, validate
    guarded, seen_validate = [], False
    for module in (jobs, validate):
        for route in module.router.routes:
            # get_type_hints: the modules use `from __future__ import annotations`, so raw annotations are strings
            # (parameters only: a RESPONSE model naming workspace_id is just output)
            if any(hasattr(m, "model_fields") and "workspace_id" in m.model_fields
                   for name, m in typing.get_type_hints(route.endpoint).items() if name != "return"):
                src = inspect.getsource(route.endpoint)
                if module is validate:
                    # the internal validate route never stores or reads it (Workspace verified it before forwarding)
                    assert ".workspace_id" not in src, "validate must not act on a request-supplied workspace_id"
                    seen_validate = True
                else:
                    assert "authorize_workspace(" in src, (
                        f"{route.endpoint.__name__} accepts a body workspace_id but never calls authorize_workspace (X-005 / FL-003)")
                    guarded.append(route.endpoint.__name__)
    assert guarded == ["create_job"] and seen_validate          # the guard really sees the routes it protects
