"""ApplyDqArtifact on POST /api/v1/projects/{id}/schema-tests (DQ renderer
2.x contract) + DQ async-mode tests run as a separate dbt test job."""
from __future__ import annotations

import time
import uuid
from types import SimpleNamespace

import httpx
import pytest
from jose import jwt

from app.core.config import get_settings
from app.core.service_auth import SERVICE_AUDIENCE
from app.main import app
from app.models import Project

SECRET = "svc-signing-secret-apply"


@pytest.fixture
def svc(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "service_jwt_secret", SECRET)
    monkeypatch.setattr(s, "internal_service_token", "legacy-raw")
    return s


@pytest.fixture
async def project_id(run_db, svc):
    async with run_db() as db:
        p = Project(org_id=svc.org_id, name="a", git_url="https://x/y.git", git_ref="main",
                    engine="pg_lake")
        db.add(p)
        await db.commit()
        return p.id


@pytest.fixture
async def api(run_db):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c


def _token(pid):
    now = int(time.time())
    return jwt.encode({"iss": "dataquality", "sub": "dataquality", "aud": SERVICE_AUDIENCE,
                       "scope": "dbt.schema-tests.write", "org_id": get_settings().org_id,
                       "project_id": str(pid), "jti": uuid.uuid4().hex, "iat": now,
                       "exp": now + 60}, SECRET, algorithm="HS256")


def _artifact(pid, **kw):
    a = {"artifact_version_id": "av-1", "binding_id": "b-1", "project_id": str(pid),
         "org_id": get_settings().org_id, "target_model": "orders",
         "dbt_schema_fragment": "version: 2\nmodels: []\n", "idempotency_key": "dq-apply:b-1:av-1",
         "content_hash": "h1", "renderer_version": "2.0.0",
         "engine_requirements": {"engine": "dbt-core", "min": "1.5.0", "max_exclusive": "2.0.0"}}
    a.update(kw)
    return a


async def _post(api, pid, body, key="dq-apply:b-1:av-1"):
    headers = {"X-Internal-Service-Token": _token(pid)}
    if key:
        headers["Idempotency-Key"] = key
    return await api.post(f"/api/v1/projects/{pid}/schema-tests", json=body, headers=headers)


async def test_apply_artifact_stored_with_meta(api, project_id, run_db):
    r = await _post(api, project_id, _artifact(project_id))
    assert r.status_code == 204, r.text
    async with run_db() as db:
        p = await db.get(Project, project_id)
        assert p.dq_schema_tests == {"orders": "version: 2\nmodels: []\n"}
        assert p.dq_schema_tests_meta["orders"]["artifact_version_id"] == "av-1"
        assert p.dq_schema_tests_meta["orders"]["idempotency_key"] == "dq-apply:b-1:av-1"


async def test_replay_is_noop_and_key_reuse_conflicts(api, project_id):
    assert (await _post(api, project_id, _artifact(project_id))).status_code == 204
    assert (await _post(api, project_id, _artifact(project_id))).status_code == 204
    r = await _post(api, project_id, _artifact(project_id, artifact_version_id="av-2",
                                               content_hash="h2"))
    assert r.status_code == 409 and r.json()["detail"]["code"] == "IDEMPOTENCY_CONFLICT"


@pytest.mark.parametrize("override,key,code,err", [
    ({"project_id": str(uuid.uuid4())}, "dq-apply:b-1:av-1", 422, "PROJECT_MISMATCH"),
    ({"org_id": "other-org"}, "dq-apply:b-1:av-1", 403, "ORG_MISMATCH"),
    ({}, "different-key", 422, "IDEMPOTENCY_KEY_MISMATCH"),
    ({"engine_requirements": {"engine": "dbt-core", "min": "9.0.0", "max_exclusive": "10.0.0"}},
     "dq-apply:b-1:av-1", 422, "ENGINE_INCOMPATIBLE"),
])
async def test_apply_artifact_rejections(api, project_id, override, key, code, err):
    r = await _post(api, project_id, _artifact(project_id, **override), key=key)
    assert r.status_code == code, r.text
    assert r.json()["detail"]["code"] == err


async def test_incomplete_apply_artifact_rejected(api, project_id):
    body = _artifact(project_id)
    del body["artifact_version_id"]
    assert (await _post(api, project_id, body)).status_code == 422


def test_engine_requirement_check():
    from app.api.projects import check_engine_requirements
    req = {"engine": "dbt-core", "min": "1.5.0", "max_exclusive": "2.0.0"}
    assert check_engine_requirements(req, "1.10.23") is None
    assert check_engine_requirements(req, "2.0.0")["code"] == "ENGINE_INCOMPATIBLE"
    assert check_engine_requirements({"engine": "spark"}, "1.10.0")
    assert check_engine_requirements(None, "1.0") is None


def test_async_dq_tests_run_as_separate_test_job(tmp_path, monkeypatch):
    from app.executors import dbt_executor as ex

    (tmp_path / "dbt_project.yml").write_text("name: p\nversion: '1'\nprofile: x\n")
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "_dq.yml").write_text(
        "version: 2\nmodels:\n- name: m\n  columns:\n  - name: id\n    tests:\n"
        "    - not_null:\n        config: {severity: warn, tags: [dq_async]}\n"
        "        meta: {dq_rule: nn}\n")
    calls = []

    def fake_run(args, project_dir, profiles_dir):
        calls.append(args)
        ok = args[0] != "test"      # the async job fails; the build must still succeed
        return SimpleNamespace(success=ok, result=SimpleNamespace(results=[]), exception=None)

    monkeypatch.setattr(ex, "_run_dbt", fake_run)
    monkeypatch.setattr(ex, "_require_pinned_dbt_utils", lambda d: False)
    monkeypatch.setattr(ex, "_force_profile", lambda d, n: None)
    out = ex.DbtExecutor().run_project(project_dir=tmp_path, engine="pg_lake", select=None)
    build = next(c for c in calls if c[0] == "build")
    assert "--exclude" in build and "tag:dq_async" in build
    assert ["test", "--select", "tag:dq_async"] in calls
    assert out["success"] is True and out["async_tests"]["success"] is False


def test_no_async_tests_means_plain_build(tmp_path, monkeypatch):
    from app.executors import dbt_executor as ex
    calls = []
    monkeypatch.setattr(ex, "_run_dbt", lambda a, p, q: calls.append(a) or SimpleNamespace(
        success=True, result=SimpleNamespace(results=[]), exception=None))
    monkeypatch.setattr(ex, "_require_pinned_dbt_utils", lambda d: False)
    monkeypatch.setattr(ex, "_force_profile", lambda d, n: None)
    out = ex.DbtExecutor().run_project(project_dir=tmp_path, engine="pg_lake", select=None)
    assert calls == [["build"]] and out["async_tests"] is None
