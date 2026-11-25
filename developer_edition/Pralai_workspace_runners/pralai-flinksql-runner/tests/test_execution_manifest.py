"""X-004: a Flink deployment freezes an immutable execution manifest and executes ONLY that.

Before: a deployment recorded the sha256 of its SQL, but the SQL it submitted, the job's name and engine were read from
the mutable `flink_jobs` row each time the deployment was (re)driven, and a Git import's provenance lived only in a
free-text marker line of `description`. Now `JobService.deploy` freezes the SQL text, name, engine, parallelism, restore
savepoint, subject, Git source and runtime versions; the submission (and a reconciler resume) reads only that.

Real, migrated Postgres, the same fake Flink Gateway as the other deploy tests, the real API for the audit route.
"""
from __future__ import annotations

import copy
import time
import uuid

import httpx
import pytest
from jose import jwt
from sqlalchemy import text, update
from sqlalchemy.exc import DBAPIError

from tests.perms import DQ_PUBLISHER, EDITOR, ENGINEER, VIEWER
from app.core.config import get_settings
from app.core.service_auth import SERVICE_AUDIENCE
from app.main import app
from app.models import FlinkDeployment, FlinkJob
from app.schemas import DeployRequest, FlinkJobCreate, FlinkJobUpdate, GitSource
from app.services import execution_manifest as em
from app.services.job_service import JobService, _execute_deploy
from tests.fakes import FakeFlinkClient, patch_flink_client

SHA1, SHA2 = "1" * 40, "2" * 40
SQL = "INSERT INTO sink SELECT * FROM src -- group ${CONSUMER_GROUP_ID};"
SECRET = "workspace-to-flink-delegation-secret"
SOURCE = {"git_url": "https://github.com/acme/flink.git", "git_ref": "main", "sql_path": "jobs/orders.sql",
          "commit_sha": SHA1, "provider": "github"}


@pytest.fixture
def flink_db(pg_db):
    return pg_db


@pytest.fixture
def no_dispatch():
    """FL-011: deploy() only persists the work (nothing starts it until the leader loop runs), so there is nothing to stop; kept so the
    tests that name it still read as \"the deploy is not executed here\"."""
    yield


async def _job(db_maker, *, sql=SQL, name=None, source=None, **kw) -> uuid.UUID:
    async with db_maker() as db:
        job = FlinkJob(org_id=get_settings().org_id, name=name or f"job-{uuid.uuid4().hex[:8]}", sql=sql,
                       status="validated", source=source, sink_engine=kw.pop("sink_engine", "trino"), **kw)
        db.add(job)
        await db.commit()
        return job.id


async def _deploy(db_maker, job_id, *, user="user-7", **req) -> uuid.UUID:
    async with db_maker() as db:
        dep = await JobService(db).deploy(get_settings().org_id, job_id, user, DeployRequest(**req))
        await db.commit()
        return dep.id


async def _dep(db_maker, dep_id) -> FlinkDeployment:
    async with db_maker() as db:
        return await db.get(FlinkDeployment, dep_id)


async def _edit_job_directly(db_maker, job_id, **values):
    """Bypass the API's edit lock: what a bug, a manual fix or a second writer could do to the row."""
    async with db_maker() as db:
        await db.execute(update(FlinkJob).where(FlinkJob.id == job_id).values(**values))
        await db.commit()


# ---------------------------------------------------------------------
# The manifest document
# ---------------------------------------------------------------------
def _manifest(**kw):
    args = {"org_id": "org", "workspace_id": "ws", "subject_id": "user-7", "job_id": "j1", "job_name": "orders",
            "job_kind": "transform", "generation": 3, "sql": SQL, "engine": "trino", "parallelism": 2,
            "from_savepoint": None, "source": SOURCE, "target_resource_id": None, "idempotency_key": "k",
            "flink_version": "1.19.1"}
    args.update(kw)
    return em.build_manifest(**args)


def test_the_manifest_records_who_what_where_and_with_which_versions():
    m = _manifest(target_resource_id="ucs://dataset")
    assert m["manifest_version"] == "1"
    assert (m["org_id"], m["workspace_id"], m["subject_id"]) == ("org", "ws", "user-7")
    assert m["job"] == {"id": "j1", "name": "orders", "kind": "transform", "generation": 3}
    assert m["sql"] == {"text": SQL, "sha256": em.sha256_text(SQL)} and m["sql_hash"] == em.sha256_text(SQL)
    assert (m["engine"], m["parallelism"], m["from_savepoint"]) == ("trino", 2, None)
    assert m["source"] == SOURCE and m["target_resource_id"] == "ucs://dataset" and m["idempotency_key"] == "k"
    assert m["runtime_version"]["flink"] == "1.19.1" and m["runtime_version"]["runner"]
    assert m["dq_artifacts"] is None                  # "not recorded", not "none"


def test_hash_is_stable_and_any_change_changes_it():
    m = _manifest()
    assert em.manifest_sha256(m) == em.manifest_sha256({k: m[k] for k in reversed(list(m))}) == em.manifest_sha256(copy.deepcopy(m))
    base = em.manifest_sha256(m)
    for change in ({"sql": SQL + " "}, {"engine": "iceberg"}, {"parallelism": 3}, {"job_name": "x"},
                   {"source": {**SOURCE, "commit_sha": SHA2}}, {"subject_id": "other"}):
        assert em.manifest_sha256(_manifest(**change)) != base


def test_verify_accepts_an_untouched_manifest_and_refuses_tampering():
    m = _manifest()
    assert em.verify(m, em.manifest_sha256(m)) is m
    changed = copy.deepcopy(m)
    changed["sql"]["text"] = "DROP TABLE x;"
    with pytest.raises(em.ManifestIntegrityError, match="hash"):
        em.verify(changed, em.manifest_sha256(m))
    with pytest.raises(em.ManifestIntegrityError, match="SQL"):                 # hash recomputed, SQL edited
        em.verify(changed, em.manifest_sha256(changed))
    for bad in ((None, "x"), (m, None)):
        with pytest.raises(em.ManifestIntegrityError):
            em.verify(*bad)
    m2 = copy.deepcopy(m)
    m2["manifest_version"] = "9"
    with pytest.raises(em.ManifestIntegrityError, match="version"):
        em.verify(m2, em.manifest_sha256(m2))


def test_the_url_never_carries_a_credential():
    assert em.redact_url("https://deploy:tok@github.com/a/b.git") == "https://github.com/a/b.git"
    assert em.redact_url("https://github.com/a/b.git") == "https://github.com/a/b.git"


# ---------------------------------------------------------------------
# Deploy freezes the manifest
# ---------------------------------------------------------------------
async def test_deploy_freezes_the_sql_settings_subject_and_source(flink_db, no_dispatch):
    ws = uuid.uuid4()
    job_id = await _job(flink_db, name="orders", source=SOURCE, workspace_id=ws, ucs_fqn="ucs://orders",
                        job_kind="quality")
    dep_id = await _deploy(flink_db, job_id, user="user-7", parallelism=4, idempotency_key="client-key")
    dep = await _dep(flink_db, dep_id)
    m = dep.manifest
    assert (m["org_id"], m["workspace_id"], m["subject_id"]) == (get_settings().org_id, str(ws), "user-7")
    assert (m["job"]["name"], m["job"]["kind"], m["job"]["generation"]) == ("orders", "quality", 1)
    assert m["sql"]["text"] == SQL and m["engine"] == "trino" and m["parallelism"] == 4
    assert m["source"] == SOURCE and m["target_resource_id"] == "ucs://orders" and m["idempotency_key"] == "client-key"
    assert dep.manifest_sha256 == em.manifest_sha256(m) and dep.sql_hash == m["sql_hash"]
    assert em.verify(m, dep.manifest_sha256)


async def test_a_retried_deploy_returns_the_same_frozen_deployment(flink_db, no_dispatch):
    job_id = await _job(flink_db)
    a = await _deploy(flink_db, job_id, idempotency_key="same")
    b = await _deploy(flink_db, job_id, idempotency_key="same")
    assert a == b


# ---------------------------------------------------------------------
# Execution reads ONLY the manifest
# ---------------------------------------------------------------------
async def test_sql_edited_after_the_deployment_was_created_is_not_what_gets_submitted(flink_db, no_dispatch, monkeypatch):
    fake = FakeFlinkClient()
    patch_flink_client(monkeypatch, fake)
    job_id = await _job(flink_db, name="orders")
    dep_id = await _deploy(flink_db, job_id)
    await _edit_job_directly(flink_db, job_id, sql="INSERT INTO other SELECT 999;")

    await _execute_deploy(dep_id)

    assert fake.execute_statements_calls == 1
    assert "INSERT INTO sink SELECT * FROM src" in fake.submitted_sql[0]
    assert "other SELECT 999" not in fake.submitted_sql[0]
    async with flink_db() as db:
        assert (await db.get(FlinkJob, job_id)).status == "running"


async def test_a_renamed_job_still_submits_under_the_frozen_name(flink_db, no_dispatch, monkeypatch):
    fake = FakeFlinkClient()
    patch_flink_client(monkeypatch, fake)
    job_id = await _job(flink_db, name="orders")
    dep_id = await _deploy(flink_db, job_id)
    await _edit_job_directly(flink_db, job_id, name="renamed")
    await _execute_deploy(dep_id)
    assert "group flink-orders" in fake.submitted_sql[0] and "flink-renamed" not in fake.submitted_sql[0]


def _normalised(sql: str) -> str:
    """Drop the per-deployment ids so two submissions can be compared."""
    import re
    return re.sub(r"inaiera-[0-9a-f]+-g\d+-[0-9a-f]+|[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}|job-[0-9a-f]{8}|-[0-9a-f]{8}\b", "<id>", sql)


async def test_a_changed_engine_does_not_change_the_submitted_catalog(flink_db, no_dispatch, monkeypatch):
    fake = FakeFlinkClient()
    patch_flink_client(monkeypatch, fake)
    plain_sql = "INSERT INTO sink SELECT * FROM src;"
    untouched = await _deploy(flink_db, await _job(flink_db, sql=plain_sql, sink_engine="trino"))
    await _execute_deploy(untouched)

    job2 = await _job(flink_db, sql=plain_sql, sink_engine="trino")
    dep2 = await _deploy(flink_db, job2)
    await _edit_job_directly(flink_db, job2, sink_engine="pg_lake")
    await _execute_deploy(dep2)
    assert _normalised(fake.submitted_sql[1]) == _normalised(fake.submitted_sql[0])


async def test_a_resumed_deployment_executes_the_same_manifest(flink_db, no_dispatch, monkeypatch):
    """A restart-resume (the reconciler calls _execute_deploy again) after the job row was changed meanwhile."""
    from tests.fakes import ProcessCrash
    fake = FakeFlinkClient(submit_mode="crash_after_submit")
    patch_flink_client(monkeypatch, fake)
    job_id = await _job(flink_db)
    dep_id = await _deploy(flink_db, job_id)
    with pytest.raises(ProcessCrash):
        await _execute_deploy(dep_id)                          # the process dies right after Flink accepted the job
    await _edit_job_directly(flink_db, job_id, sql="INSERT INTO other SELECT 999;")
    fake.submit_mode = "ok"
    fake.jobs_by_name.clear()
    fake.job_states.clear()
    async with flink_db() as db:                               # the reconciler resubmits a deployment that never submitted
        await db.execute(update(FlinkDeployment).where(FlinkDeployment.id == dep_id).values(
            gateway_operation_handle=None, submission_tag=None))
        await db.commit()
    await _execute_deploy(dep_id)
    assert all("other SELECT 999" not in sql for sql in fake.submitted_sql)
    assert fake.execute_statements_calls == 2


async def test_a_tampered_manifest_is_refused_and_nothing_is_submitted(flink_db, no_dispatch, monkeypatch):
    """The trigger normally prevents this; it is switched off here to prove the application re-checks anyway."""
    fake = FakeFlinkClient()
    patch_flink_client(monkeypatch, fake)
    job_id = await _job(flink_db)
    dep_id = await _deploy(flink_db, job_id)
    tampered = copy.deepcopy((await _dep(flink_db, dep_id)).manifest)
    tampered["sql"]["text"] = "INSERT INTO evil SELECT 1;"
    async with flink_db() as db:
        await db.execute(text("ALTER TABLE flink_deployments DISABLE TRIGGER trg_flink_deployments_manifest_immutable"))
        await db.execute(update(FlinkDeployment).where(FlinkDeployment.id == dep_id).values(manifest=tampered))
        await db.execute(text("ALTER TABLE flink_deployments ENABLE TRIGGER trg_flink_deployments_manifest_immutable"))
        await db.commit()

    await _execute_deploy(dep_id)

    assert fake.execute_statements_calls == 0
    dep = await _dep(flink_db, dep_id)
    assert dep.status == "failed" and "refusing to deploy" in dep.error_message


async def test_a_deployment_created_by_an_older_runner_still_runs_and_still_refuses_changed_sql(flink_db, no_dispatch,
                                                                                                monkeypatch):
    fake = FakeFlinkClient()
    patch_flink_client(monkeypatch, fake)
    ok_job = await _job(flink_db)
    ok = await _deploy(flink_db, ok_job)
    async with flink_db() as db:                                # what an older runner left: no manifest
        await db.execute(text("ALTER TABLE flink_deployments DISABLE TRIGGER trg_flink_deployments_manifest_immutable"))
        await db.execute(update(FlinkDeployment).where(FlinkDeployment.id == ok).values(manifest=None, manifest_sha256=None))
        await db.execute(text("ALTER TABLE flink_deployments ENABLE TRIGGER trg_flink_deployments_manifest_immutable"))
        await db.commit()
    await _execute_deploy(ok)
    assert fake.execute_statements_calls == 1

    changed_job = await _job(flink_db)
    changed = await _deploy(flink_db, changed_job)
    async with flink_db() as db:
        await db.execute(text("ALTER TABLE flink_deployments DISABLE TRIGGER trg_flink_deployments_manifest_immutable"))
        await db.execute(update(FlinkDeployment).where(FlinkDeployment.id == changed).values(manifest=None, manifest_sha256=None))
        await db.execute(text("ALTER TABLE flink_deployments ENABLE TRIGGER trg_flink_deployments_manifest_immutable"))
        await db.commit()
    await _edit_job_directly(flink_db, changed_job, sql="INSERT INTO other SELECT 999;")
    await _execute_deploy(changed)
    assert fake.execute_statements_calls == 1                   # nothing new submitted
    assert "SQL changed" in (await _dep(flink_db, changed)).error_message


async def test_the_database_refuses_to_change_a_frozen_manifest(flink_db, no_dispatch):
    dep_id = await _deploy(flink_db, await _job(flink_db))
    for values in ({"manifest": {"x": 1}}, {"manifest_sha256": "0" * 64}, {"sql_hash": "other"}, {"manifest": None}):
        async with flink_db() as db:
            with pytest.raises(DBAPIError, match="immutable"):
                await db.execute(update(FlinkDeployment).where(FlinkDeployment.id == dep_id).values(**values))
    async with flink_db() as db:                                # everything else still updates
        await db.execute(update(FlinkDeployment).where(FlinkDeployment.id == dep_id).values(status="running", error_message="x"))
        await db.commit()


# ---------------------------------------------------------------------
# A job's Git source
# ---------------------------------------------------------------------
async def _create(flink_db, **kw):
    async with flink_db() as db:
        job = await JobService(db).create(org_id=get_settings().org_id, user_id="u", req=FlinkJobCreate(
            name=f"j-{uuid.uuid4().hex[:6]}", sql=SQL, **kw))
        await db.commit()
        return job


async def test_a_created_job_records_its_git_source_without_credentials(flink_db):
    job = await _create(flink_db, source=GitSource(git_url="https://deploy:tok-1@github.com/acme/flink.git",
                                                   git_ref="main", sql_path="a.sql", commit_sha=SHA1.upper(), provider="github"))
    assert job.source == {"git_url": "https://github.com/acme/flink.git", "git_ref": "main", "sql_path": "a.sql",
                          "commit_sha": SHA1, "provider": "github", "requested_ref": None, "imported_at": None}
    assert "tok-1" not in repr(job.source)


async def test_a_job_made_by_hand_has_no_source(flink_db):
    assert (await _create(flink_db)).source is None


@pytest.mark.parametrize("bad", ["", "abc123", "g" * 40, "1" * 39, "1" * 41, "main"])
def test_the_commit_must_be_a_full_sha(bad):
    with pytest.raises(ValueError):
        GitSource(git_url="https://github.com/a/b.git", commit_sha=bad)


async def test_editing_the_sql_without_a_new_source_clears_the_recorded_source(flink_db):
    job = await _create(flink_db, source=GitSource(git_url="https://github.com/a/b.git", commit_sha=SHA1))
    async with flink_db() as db:
        edited = await JobService(db).update(get_settings().org_id, job.id, FlinkJobUpdate(sql="INSERT INTO x SELECT 2;"))
        await db.commit()
    assert edited.source is None                       # the SQL is no longer from that commit


async def test_a_resync_records_the_new_commit_and_other_edits_keep_the_source(flink_db):
    job = await _create(flink_db, source=GitSource(git_url="https://github.com/a/b.git", commit_sha=SHA1))
    async with flink_db() as db:
        synced = await JobService(db).update(get_settings().org_id, job.id, FlinkJobUpdate(
            sql="INSERT INTO x SELECT 2;", source=GitSource(git_url="https://github.com/a/b.git", commit_sha=SHA2)))
        await db.commit()
    assert synced.source["commit_sha"] == SHA2
    async with flink_db() as db:
        renamed = await JobService(db).update(get_settings().org_id, job.id, FlinkJobUpdate(description="just a note"))
        await db.commit()
    assert renamed.source["commit_sha"] == SHA2        # a description edit does not touch provenance


async def test_the_deployment_manifest_carries_the_source_the_job_had_at_that_moment(flink_db, no_dispatch):
    job = await _create(flink_db, source=GitSource(git_url="https://github.com/a/b.git", commit_sha=SHA1))
    async with flink_db() as db:
        await db.execute(update(FlinkJob).where(FlinkJob.id == job.id).values(status="validated"))
        await db.commit()
    first = await _deploy(flink_db, job.id)
    async with flink_db() as db:                        # the first deployment ends; the job is editable again
        await db.execute(update(FlinkJob).where(FlinkJob.id == job.id).values(status="failed"))
        await db.execute(update(FlinkDeployment).where(FlinkDeployment.id == first).values(status="failed"))
        await db.commit()
    async with flink_db() as db:
        await JobService(db).update(get_settings().org_id, job.id, FlinkJobUpdate(
            sql="INSERT INTO x SELECT 2;", source=GitSource(git_url="https://github.com/a/b.git", commit_sha=SHA2)))
        await db.commit()
    async with flink_db() as db:
        await db.execute(update(FlinkJob).where(FlinkJob.id == job.id).values(status="validated"))
        await db.commit()
    second = await _deploy(flink_db, job.id)
    assert (await _dep(flink_db, first)).manifest["source"]["commit_sha"] == SHA1
    assert (await _dep(flink_db, second)).manifest["source"]["commit_sha"] == SHA2
    assert (await _dep(flink_db, second)).manifest["sql"]["text"] == "INSERT INTO x SELECT 2;"


# ---------------------------------------------------------------------
# The audit route
# ---------------------------------------------------------------------
def _token(scope="flink.jobs.read", **over) -> str:
    now = int(time.time())
    claims = {"iss": "workspace", "sub": "user-42", "act": {"sub": "workspace"}, "aud": SERVICE_AUDIENCE, "scope": scope,
              "org_id": get_settings().org_id, "permissions": ENGINEER, "email": "u@example.com",
              "jti": uuid.uuid4().hex, "iat": now, "exp": now + 60}
    claims.update(over)
    return jwt.encode(claims, SECRET, algorithm="HS256")


@pytest.fixture
def api(flink_db, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "delegation_jwt_secret", SECRET)
    monkeypatch.setattr(s, "delegation_jwt_allowed_issuers", ["workspace"])

    class Api:
        async def get(self, path, token=None):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
                headers = {"Authorization": f"Bearer {token or _token()}"}
                return await c.get(path, headers=headers)
    return Api()


async def test_the_manifest_route_returns_the_audit_record(flink_db, no_dispatch, api):
    job_id = await _job(flink_db, name="orders", source=SOURCE)
    dep_id = await _deploy(flink_db, job_id, user="user-7")
    r = await api.get(f"/api/v1/jobs/{job_id}/deployments/{dep_id}/manifest")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["manifest"]["sql"]["text"] == SQL and body["manifest"]["source"]["commit_sha"] == SHA1
    assert body["manifest_sha256"] == em.manifest_sha256(body["manifest"]) and body["generation"] == 1


async def test_the_manifest_route_is_scoped_and_authenticated(flink_db, no_dispatch, api):
    job_id = await _job(flink_db)
    dep_id = await _deploy(flink_db, job_id)
    other_job = await _job(flink_db)
    path = f"/api/v1/jobs/{job_id}/deployments/{dep_id}/manifest"
    assert (await api.get(f"/api/v1/jobs/{other_job}/deployments/{dep_id}/manifest")).status_code == 404   # not this job's
    assert (await api.get(f"/api/v1/jobs/{job_id}/deployments/{uuid.uuid4()}/manifest")).status_code == 404
    assert (await api.get(f"/api/v1/jobs/{uuid.uuid4()}/deployments/{dep_id}/manifest")).status_code == 404
    assert (await api.get(path, _token(scope="flink.catalog.read"))).status_code == 403                     # wrong scope
    assert (await api.get(path, _token(org_id="another-org"))).status_code in (401, 403, 404)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        assert (await c.get(path)).status_code == 401
