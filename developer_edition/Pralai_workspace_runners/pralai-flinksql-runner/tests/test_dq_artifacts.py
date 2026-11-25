"""X-004: a QUALITY job records which DQ artifact its SQL was generated from, and every deployment freezes it.

Before: the Data Quality service created the quality job but never said which artifact it came from, so every deployment's
manifest said `dq_artifacts: null` ("not recorded"). Now DQ sends a record (artifact version, binding, content hash, renderer
version, ruleset and its version); the runner stores it on the job and freezes it into each deployment's manifest. Only DQ may
assert it (a user, or Workspace on a user's behalf, cannot claim a job came from a DQ artifact), only on a quality job, and
new SQL without a new record clears it (the SQL is no longer from that artifact).
"""
from __future__ import annotations

import time
import uuid

import httpx
import pytest
from jose import jwt
from sqlalchemy import update

from tests.perms import DQ_PUBLISHER, EDITOR, ENGINEER, VIEWER
from app.core.config import get_settings
from app.core.service_auth import SERVICE_AUDIENCE
from app.main import app
from app.models import FlinkDeployment, FlinkJob
from app.schemas import FlinkJobCreate, FlinkJobUpdate
from app.services import execution_manifest as em
from app.services.job_service import JobService
from tests.test_execution_manifest import SQL, _dep, _deploy, flink_db, no_dispatch  # noqa: F401  (fixtures)

DQ_SECRET = "dq-to-flink-delegation-secret-different"
WORKSPACE_SECRET = "workspace-to-flink-delegation-secret"
H1, H2 = "a" * 64, "b" * 64
A1, B1 = str(uuid.uuid4()), str(uuid.uuid4())


def record(**over) -> dict:
    return {"artifact_version_id": A1, "binding_id": B1, "content_hash": H1, "renderer_version": "2.0.0",
            "ruleset_id": str(uuid.uuid4()), "ruleset_version": 3, "manifest_sha256": H2, **over}


async def _create(db_maker, *, dq=None, kind="quality", name=None, sql=SQL) -> uuid.UUID:
    async with db_maker() as db:
        job = await JobService(db).create(org_id=get_settings().org_id, user_id="dq-user", req=FlinkJobCreate(
            name=name or f"dq-quality-{uuid.uuid4().hex[:6]}", sql=sql, job_kind=kind,
            ucs_fqn="pg.public.orders" if kind == "quality" else None, dq_artifacts=dq))
        await db.commit()
        return job.id


async def _set_validated(db_maker, job_id):
    async with db_maker() as db:
        await db.execute(update(FlinkJob).where(FlinkJob.id == job_id).values(status="validated"))
        await db.commit()


async def _job_row(db_maker, job_id) -> FlinkJob:
    async with db_maker() as db:
        return await db.get(FlinkJob, job_id)


# ── stored, and frozen into the manifest ─────────────────────────────────
async def test_a_quality_job_stores_the_record_it_was_created_with(flink_db):
    rec = record()
    job_id = await _create(flink_db, dq=rec)
    assert (await _job_row(flink_db, job_id)).dq_artifacts == rec


async def test_a_deployment_freezes_the_dq_record_into_its_manifest(flink_db, no_dispatch):
    rec = record()
    job_id = await _create(flink_db, dq=rec)
    await _set_validated(flink_db, job_id)
    dep = await _dep(flink_db, await _deploy(flink_db, job_id))
    assert dep.manifest["dq_artifacts"] == rec
    em.verify(dep.manifest, dep.manifest_sha256)                      # hashes like any other manifest


async def test_a_job_with_no_record_says_not_recorded(flink_db, no_dispatch):
    job_id = await _create(flink_db, kind="transform")
    await _set_validated(flink_db, job_id)
    dep = await _dep(flink_db, await _deploy(flink_db, job_id))
    assert dep.manifest["dq_artifacts"] is None


async def test_a_later_artifact_does_not_rewrite_an_earlier_deployments_manifest(flink_db, no_dispatch):
    job_id = await _create(flink_db, dq=record())
    await _set_validated(flink_db, job_id)
    first = await _deploy(flink_db, job_id)
    async with flink_db() as db:                                       # the first deployment ends; the job is editable again
        await db.execute(update(FlinkJob).where(FlinkJob.id == job_id).values(status="failed"))
        await db.execute(update(FlinkDeployment).where(FlinkDeployment.id == first).values(status="failed"))
        await db.commit()
    async with flink_db() as db:
        await JobService(db).update(get_settings().org_id, job_id, FlinkJobUpdate(
            sql="INSERT INTO x SELECT 2;", dq_artifacts=record(artifact_version_id=str(uuid.uuid4()), content_hash=H2)))
        await db.commit()
    await _set_validated(flink_db, job_id)
    second = await _deploy(flink_db, job_id)
    assert (await _dep(flink_db, first)).manifest["dq_artifacts"]["content_hash"] == H1
    assert (await _dep(flink_db, second)).manifest["dq_artifacts"]["content_hash"] == H2


# ── a record describes the SQL it came with ──────────────────────────────
async def test_new_sql_without_a_new_record_clears_it(flink_db):
    job_id = await _create(flink_db, dq=record())
    async with flink_db() as db:
        await JobService(db).update(get_settings().org_id, job_id, FlinkJobUpdate(sql="INSERT INTO y SELECT 1;"))
        await db.commit()
    assert (await _job_row(flink_db, job_id)).dq_artifacts is None


async def test_new_sql_with_a_new_record_replaces_it(flink_db):
    job_id = await _create(flink_db, dq=record())
    new = record(content_hash=H2)
    async with flink_db() as db:
        await JobService(db).update(get_settings().org_id, job_id,
                                    FlinkJobUpdate(sql="INSERT INTO y SELECT 1;", dq_artifacts=new))
        await db.commit()
    assert (await _job_row(flink_db, job_id)).dq_artifacts == new


async def test_an_edit_that_leaves_the_sql_alone_keeps_the_record(flink_db):
    rec = record()
    job_id = await _create(flink_db, dq=rec)
    async with flink_db() as db:
        await JobService(db).update(get_settings().org_id, job_id, FlinkJobUpdate(description="renamed in the UI"))
        await db.commit()
    assert (await _job_row(flink_db, job_id)).dq_artifacts == rec


async def test_dq_can_reassert_the_record_for_identical_sql(flink_db):
    job_id = await _create(flink_db, dq=record())
    new = record(content_hash=H2)
    async with flink_db() as db:
        await JobService(db).update(get_settings().org_id, job_id, FlinkJobUpdate(sql=SQL, dq_artifacts=new))
        await db.commit()
    assert (await _job_row(flink_db, job_id)).dq_artifacts == new


# ── the API: only DQ, only a quality job, strict shape ───────────────────
def _token(secret, iss, scope="flink.jobs.edit", **over) -> str:
    now = int(time.time())
    claims = {"iss": iss, "sub": "user-42", "act": {"sub": iss}, "aud": SERVICE_AUDIENCE, "scope": scope,
              "org_id": get_settings().org_id, "permissions": ENGINEER, "email": "u@example.com",
              "jti": uuid.uuid4().hex, "iat": now, "exp": now + 60}
    claims.update(over)
    return jwt.encode(claims, secret, algorithm="HS256")


@pytest.fixture
def api(flink_db, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "delegation_jwt_secret", WORKSPACE_SECRET)
    monkeypatch.setattr(s, "delegation_jwt_allowed_issuers", ["workspace"])
    monkeypatch.setattr(s, "dq_delegation_jwt_secret", DQ_SECRET)

    class Api:
        async def call(self, method, path, body, token):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
                return await c.request(method, path, json=body, headers={"Authorization": f"Bearer {token}"})

        def dq(self):
            return _token(DQ_SECRET, "dataquality")

        def workspace(self):
            return _token(WORKSPACE_SECRET, "workspace")
    return Api()


def _body(**over):
    return {"name": f"dq-quality-{uuid.uuid4().hex[:6]}", "sql": SQL, "job_kind": "quality", "ucs_fqn": "pg.public.orders",
            "dq_artifacts": record(), **over}


async def test_dq_can_create_a_quality_job_with_its_record(api):
    r = await api.call("POST", "/api/v1/jobs", _body(), api.dq())
    assert r.status_code == 201, r.text
    assert r.json()["dq_artifacts"]["content_hash"] == H1 and r.json()["job_kind"] == "quality"


async def test_a_user_cannot_claim_a_dq_record_even_through_workspace(api):
    r = await api.call("POST", "/api/v1/jobs", _body(), api.workspace())
    assert r.status_code == 422 and r.json()["detail"]["code"] == "DQ_ARTIFACTS_NOT_ACCEPTED"


async def test_a_job_that_is_not_a_quality_job_has_no_dq_record(api):
    r = await api.call("POST", "/api/v1/jobs", _body(job_kind="transform", ucs_fqn=None), api.dq())
    assert r.status_code == 422 and r.json()["detail"]["code"] == "DQ_ARTIFACTS_NOT_APPLICABLE"


@pytest.mark.parametrize("bad", [record(content_hash="short"), record(extra_field="x"), record(ruleset_version=0),
                                 {k: v for k, v in record().items() if k != "artifact_version_id"}],
                         ids=["bad-hash", "unknown-field", "bad-version", "missing-artifact"])
async def test_the_record_has_a_strict_shape(api, bad):
    r = await api.call("POST", "/api/v1/jobs", _body(dq_artifacts=bad), api.dq())
    assert r.status_code == 422


async def test_dq_can_update_the_record_and_a_user_cannot(api, flink_db):
    job_id = await _create(flink_db, dq=record())
    ok = await api.call("PATCH", f"/api/v1/jobs/{job_id}", {"sql": "INSERT INTO y SELECT 1;", "dq_artifacts": record(content_hash=H2)}, api.dq())
    assert ok.status_code == 200 and ok.json()["dq_artifacts"]["content_hash"] == H2
    bad = await api.call("PATCH", f"/api/v1/jobs/{job_id}", {"dq_artifacts": record()}, api.workspace())
    assert bad.status_code == 422 and bad.json()["detail"]["code"] == "DQ_ARTIFACTS_NOT_ACCEPTED"


async def test_a_plain_user_edit_of_a_dq_job_still_works_and_clears_the_record(api, flink_db):
    job_id = await _create(flink_db, dq=record())
    r = await api.call("PATCH", f"/api/v1/jobs/{job_id}", {"sql": "INSERT INTO y SELECT 1;"}, api.workspace())
    assert r.status_code == 200 and r.json()["dq_artifacts"] is None


# ── W-022 / W-015 ────────────────────────────────────────────────────────
async def test_renaming_a_job_onto_an_existing_name_is_a_409_not_an_internal_error(flink_db):
    from fastapi import HTTPException

    first = await _create(flink_db, kind="transform", name="taken-name")
    second = await _create(flink_db, kind="transform", name="other-name")
    async with flink_db() as db:
        with pytest.raises(HTTPException) as exc:
            await JobService(db).update(get_settings().org_id, second, FlinkJobUpdate(name="taken-name"))
        assert exc.value.status_code == 409 and exc.value.detail["code"] == "JOB_NAME_TAKEN"
        await db.rollback()
    assert (await _job_row(flink_db, second)).name == "other-name" and first != second
    async with flink_db() as db:                                   # a free name still works
        job = await JobService(db).update(get_settings().org_id, second, FlinkJobUpdate(name="free-name"))
        assert job.name == "free-name"


async def test_the_structured_source_keeps_the_requested_ref_and_the_import_time(flink_db):
    from datetime import datetime, timezone

    from app.schemas import GitSource
    when = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
    async with flink_db() as db:
        job = await JobService(db).create(org_id=get_settings().org_id, user_id="u", req=FlinkJobCreate(
            name=f"src-{uuid.uuid4().hex[:6]}", sql=SQL, source=GitSource(
                git_url="https://github.com/acme/repo", git_ref="main", requested_ref=None, sql_path="job.sql",
                commit_sha="a" * 40, provider="github", imported_at=when)))
        await db.commit()
        assert job.source == {"git_url": "https://github.com/acme/repo", "git_ref": "main", "sql_path": "job.sql",
                              "commit_sha": "a" * 40, "provider": "github", "requested_ref": None,
                              "imported_at": when.isoformat()}


async def test_a_source_recorded_before_the_new_fields_still_validates():
    from app.schemas import GitSource
    s = GitSource(git_url="https://github.com/a/b", git_ref="main", sql_path="x.sql", commit_sha="b" * 40)
    assert s.requested_ref is None and s.imported_at is None
