"""DG-009: the runner resolves a project's current commit for Dagster's git sensor.

The sensor used to run `git ls-remote` itself in the Dagster code server, which holds no Git credentials (the chart's advertised
`codeServer.gitCredentials` was mounted nowhere) and, since DBT-009, cannot learn a project's `credential_ref`. Now it asks the runner:
`GET /internal/projects/{id}/head` — same resolver as a run submission, same credentials and egress.
"""
from __future__ import annotations

import time
import uuid

import httpx
import pytest
from jose import jwt

from app.core.config import get_settings
from app.main import app
from app.models import Project
from app.services import run_manifest
from tests.conftest import FAKE_COMMIT_SHA

SECRET = "dagster-scheduler-signing-secret"


@pytest.fixture(autouse=True)
def _identity(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "scheduler_jwt_secret", SECRET)
    monkeypatch.setattr(s, "scheduler_jwt_allowed_issuers", ["dagster"])


@pytest.fixture
async def api(run_db):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _token(scope="dbt.projects.read") -> str:
    now = int(time.time())
    return jwt.encode({"iss": "dagster", "sub": "dagster", "aud": "dbtrunner", "scope": scope, "org_id": get_settings().org_id,
                       "jti": uuid.uuid4().hex, "iat": now, "exp": now + 60}, SECRET, algorithm="HS256")


async def _head(api, pid, scope="dbt.projects.read"):
    return await api.get(f"/api/v1/internal/projects/{pid}/head", headers={"X-Internal-Service-Token": _token(scope)})


async def _project(run_db, *, org=None, ref="main", credential_ref=None, url="https://github.com/acme/private.git") -> Project:
    async with run_db() as db:
        p = Project(org_id=org or get_settings().org_id, name=f"p-{uuid.uuid4().hex[:6]}", git_url=url, git_ref=ref,
                    engine="pg_lake", credential_ref=credential_ref)
        db.add(p)
        await db.commit()
        return p


async def test_the_head_is_the_resolved_commit_of_the_projects_ref(api, run_db):
    p = await _project(run_db, ref="release")
    r = await _head(api, p.id)
    assert r.status_code == 200, r.text
    assert r.json() == {"project_id": str(p.id), "git_ref": "release", "sha": FAKE_COMMIT_SHA}


async def test_the_runner_resolves_with_the_projects_own_credential_and_url(api, run_db, monkeypatch):
    """The point of the finding: the runner — which holds the credential — does the resolving, with the token behind `credential_ref`."""
    monkeypatch.setenv("GIT_CREDENTIAL_ACME_GH", "tok-123")
    seen = {}

    async def spy(git_url, git_ref, token=None):
        seen.update(git_url=git_url, git_ref=git_ref, token=token)
        return FAKE_COMMIT_SHA
    monkeypatch.setattr("app.services.run_manifest.resolve_commit", spy)
    p = await _project(run_db, credential_ref="acme_gh", ref="dev")
    assert (await _head(api, p.id)).status_code == 200
    assert seen == {"git_url": "https://github.com/acme/private.git", "git_ref": "dev", "token": "tok-123"}


async def test_a_project_without_a_credential_is_resolved_without_a_token(api, run_db, monkeypatch):
    seen = {}

    async def spy(git_url, git_ref, token=None):
        seen["token"] = token
        return FAKE_COMMIT_SHA
    monkeypatch.setattr("app.services.run_manifest.resolve_commit", spy)
    p = await _project(run_db)
    await _head(api, p.id)
    assert seen["token"] is None


@pytest.mark.parametrize("code,status", [("GIT_REF_NOT_FOUND", 422), ("GIT_UNREACHABLE", 502)])
async def test_a_ref_that_cannot_be_resolved_answers_like_a_submission(api, run_db, monkeypatch, code, status):
    async def boom(git_url, git_ref, token=None):
        raise run_manifest.GitRefError(code, "cannot read https://github.com/acme/private.git")
    monkeypatch.setattr("app.services.run_manifest.resolve_commit", boom)
    p = await _project(run_db)
    r = await _head(api, p.id)
    assert r.status_code == status and r.json()["detail"]["code"] == code


async def test_another_orgs_project_is_not_found(api, run_db):
    p = await _project(run_db, org="some-other-org")
    assert (await _head(api, p.id)).status_code == 404


async def test_an_unknown_project_is_not_found(api):
    assert (await _head(api, uuid.uuid4())).status_code == 404


async def test_the_route_needs_the_scheduler_identity_and_its_scope(api, run_db):
    p = await _project(run_db)
    assert (await api.get(f"/api/v1/internal/projects/{p.id}/head")).status_code == 401
    assert (await _head(api, p.id, scope="dbt.runs.read")).status_code == 403


async def test_the_database_connection_is_not_held_while_git_is_read(api, run_db, monkeypatch):
    """ls-remote can take seconds (the resolve timeout): the read transaction must be over by then, or every slow Git host pins a pooled
    connection for the sensor's whole tick."""
    pool = run_db.kw["bind"].sync_engine.pool
    p = await _project(run_db)
    held = {}

    async def slow(git_url, git_ref, token=None):
        held["connections"] = pool.checkedout()
        return FAKE_COMMIT_SHA
    monkeypatch.setattr("app.services.run_manifest.resolve_commit", slow)
    assert (await _head(api, p.id)).status_code == 200
    assert held["connections"] == 0


def test_the_real_resolver_is_exact_about_a_nested_branch_ending_in_main(tmp_path):
    """What the sensor's own resolver used to be tested for, now against the resolver the sensor actually relies on."""
    from tests.test_run_manifest import _git
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    (work / "f").write_text("one\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "one")
    _git(work, "checkout", "-q", "-b", "feature/main")                       # sorts BEFORE main and ends in "main"
    (work / "f").write_text("feature\n")
    _git(work, "commit", "-qam", "feature")
    feature = _git(work, "rev-parse", "HEAD")
    _git(work, "checkout", "-q", "main")
    (work / "f").write_text("two\n")
    _git(work, "commit", "-qam", "two")
    main = _git(work, "rev-parse", "HEAD")
    bare = tmp_path / "origin.git"
    _git(tmp_path, "clone", "-q", "--bare", str(work), str(bare))
    assert run_manifest.resolve_commit_blocking(f"file://{bare}", "main") == main != feature
    assert run_manifest.resolve_commit_blocking(f"file://{bare}", "feature/main") == feature
