"""RUN-FLINK-013 — restart- and replica-safe deploy/stop lifecycle.

Proves, against the throwaway migrated Postgres (tests/conftest.py pg_db —
TEST_DATABASE_URL only, refuses anything not named *_test) with an
in-memory fake Flink:

  * deploy()/stop() persist desired state, a per-job generation and the
    SQL artifact hash, and atomically claim the job;
  * the submission handle (submission_tag / Gateway session + operation
    handle / submit time) is durable BEFORE a crash can happen, so a
    process that dies right after the Gateway accepted the job is
    re-attached by the reconciler — never resubmitted;
  * a transient JobManager outage while 'pausing' never marks the job
    paused; a failed stop leaves job/deployment consistent with Flink;
  * the reconciler runs on one replica (advisory-lock leader election) and
    only acts on deployments whose lease has expired, after re-claiming.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from app.models import FlinkDeployment, FlinkJob, FlinkSavepoint
from app.schemas import DeployRequest, StopRequest
from app.services import deployment_reconciler
from app.services.coordination import LeaderElector
from app.services.job_service import (
    JobService,
    _ACTIVE_DEPLOYMENT_IDS,
    _execute_deploy,
    _execute_stop,
    sql_artifact_hash,
)
from tests.fakes import FakeFlinkClient, ProcessCrash, leader_tick, patch_flink_client

SQL = "INSERT INTO t SELECT 1;"


@pytest.fixture
def flink_db(pg_db):
    return pg_db


async def _make_job(flink_db, *, status: str, sql: str = SQL) -> uuid.UUID:
    async with flink_db() as db:
        job = FlinkJob(org_id="org1", name=f"job-{uuid.uuid4().hex[:8]}", sql=sql, status=status)
        db.add(job)
        await db.commit()
        return job.id


async def _make_deployment(
    flink_db, job_id: uuid.UUID, *, flink_job_id: str | None = None,
    stop_savepoint_dir: str | None = None, status: str = "submitting", **extra,
) -> uuid.UUID:
    async with flink_db() as db:
        deployment = FlinkDeployment(
            job_id=job_id, trigger="manual", flink_job_id=flink_job_id,
            stop_savepoint_dir=stop_savepoint_dir, status=status, generation=1,
            sql_hash=sql_artifact_hash(SQL), **extra,
        )
        db.add(deployment)
        await db.commit()
        return deployment.id


async def _get(flink_db, model, id_):
    async with flink_db() as db:
        return await db.get(model, id_)


async def _wait_for(flink_db, job_id, status, timeout=8.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    job = None
    while loop.time() < deadline:
        job = await _get(flink_db, FlinkJob, job_id)
        if job.status == status:
            return job
        await leader_tick()        # FL-011: the leader's loop is what starts the persisted deploy/stop
        await asyncio.sleep(0.05)
    raise AssertionError(f"job never reached {status!r} (last {job and job.status!r})")


# ---------------------------------------------------------------------
# deploy()/stop() — desired state, generation, hash, atomic claim
# ---------------------------------------------------------------------
async def test_deploy_persists_desired_state_generation_and_sql_hash(flink_db, monkeypatch):
    fake = FakeFlinkClient()
    patch_flink_client(monkeypatch, fake)
    job_id = await _make_job(flink_db, status="validated")

    async with flink_db() as db:
        deployment = await JobService(db).deploy("org1", job_id, "user1", DeployRequest(parallelism=4))
        await db.commit()
        assert deployment.parallelism == 4
        assert deployment.generation == 1
        assert deployment.sql_hash == sql_artifact_hash(SQL)

    job = await _wait_for(flink_db, job_id, "running")
    assert job.generation_counter == 1
    dep = await _get(flink_db, FlinkDeployment, deployment.id)
    assert dep.flink_job_id == "job-1"
    assert dep.submission_tag and dep.submission_tag in fake.submitted_sql[0]
    assert dep.gateway_operation_handle == "op-1"
    assert dep.submit_started_at is not None
    assert dep.owner_id is None  # lease released when the attempt finished


async def test_generation_increments_and_savepoint_is_tied_to_generation_and_hash(flink_db, monkeypatch):
    fake = FakeFlinkClient()
    patch_flink_client(monkeypatch, fake)
    job_id = await _make_job(flink_db, status="validated")

    async with flink_db() as db:
        d1 = await JobService(db).deploy("org1", job_id, "u", DeployRequest())
        await db.commit()
    await _wait_for(flink_db, job_id, "running")
    async with flink_db() as db:
        await JobService(db).stop("org1", job_id, StopRequest(savepoint_path="s3://sp"))
        await db.commit()
    job = await _wait_for(flink_db, job_id, "paused")
    assert job.savepoint_generation == 1
    assert job.savepoint_sql_hash == sql_artifact_hash(SQL)
    async with flink_db() as db:
        sps = (await db.execute(select(FlinkSavepoint).where(FlinkSavepoint.job_id == job_id))).scalars().all()
    assert [(s.generation, s.sql_hash, s.deployment_id) for s in sps] == [(1, sql_artifact_hash(SQL), d1.id)]

    async with flink_db() as db:
        d2 = await JobService(db).deploy("org1", job_id, "u", DeployRequest())
        await db.commit()
        assert d2.generation == 2
        assert d2.from_savepoint == job.savepoint_path  # same artifact -> restored


async def test_savepoint_from_a_different_sql_artifact_is_refused(flink_db, monkeypatch):
    patch_flink_client(monkeypatch, FakeFlinkClient())
    job_id = await _make_job(flink_db, status="paused")
    async with flink_db() as db:
        db.add(FlinkSavepoint(job_id=job_id, generation=1, sql_hash="0" * 64,
                              path="s3://sp/old", kind="savepoint"))
        await db.commit()
    from fastapi import HTTPException
    async with flink_db() as db:
        with pytest.raises(HTTPException) as exc:
            await JobService(db).deploy("org1", job_id, "u", DeployRequest(from_savepoint="s3://sp/old"))
    assert exc.value.status_code == 422


async def test_stale_job_savepoint_from_other_sql_is_not_restored(flink_db, monkeypatch):
    patch_flink_client(monkeypatch, FakeFlinkClient())
    job_id = await _make_job(flink_db, status="paused")
    async with flink_db() as db:
        job = await db.get(FlinkJob, job_id)
        job.savepoint_path = "s3://sp/stale"
        job.savepoint_sql_hash = "f" * 64
        await db.commit()
    async with flink_db() as db:
        dep = await JobService(db).deploy("org1", job_id, "u", DeployRequest())
        await db.commit()
        assert dep.from_savepoint is None


async def test_concurrent_deploy_calls_only_one_wins_the_claim(flink_db, monkeypatch):
    fake = FakeFlinkClient()
    patch_flink_client(monkeypatch, fake)
    job_id = await _make_job(flink_db, status="validated")

    async def _one_deploy_attempt():
        async with flink_db() as db:
            try:
                await JobService(db).deploy("org1", job_id, "user1", DeployRequest())
                await db.commit()
                return "won"
            except Exception:
                return "lost"

    results = await asyncio.gather(_one_deploy_attempt(), _one_deploy_attempt())
    assert sorted(results) == ["lost", "won"]
    await _wait_for(flink_db, job_id, "running")
    assert fake.execute_statements_calls == 1
    async with flink_db() as db:
        deployments = (await db.execute(
            select(FlinkDeployment).where(FlinkDeployment.job_id == job_id))).scalars().all()
    assert len(deployments) == 1 and deployments[0].status == "running"


async def test_stop_persists_savepoint_dir_and_trigger_and_claims_the_job(flink_db, monkeypatch):
    fake = FakeFlinkClient(job_states={"live-job-1": "RUNNING"})
    patch_flink_client(monkeypatch, fake)
    job_id = await _make_job(flink_db, status="running")
    deployment_id = await _make_deployment(flink_db, job_id, flink_job_id="live-job-1", status="running")

    async with flink_db() as db:
        dep = await JobService(db).stop("org1", job_id, StopRequest(savepoint_path="s3://custom/dir"))
        await db.commit()
        assert dep.stop_savepoint_dir == "s3://custom/dir"

    await _wait_for(flink_db, job_id, "paused")
    assert fake.stop_with_savepoint_calls == 1
    dep = await _get(flink_db, FlinkDeployment, deployment_id)
    assert dep.stop_trigger_id == "trigger-1"
    assert dep.status == "paused"


# ---------------------------------------------------------------------
# RUN-FLINK-011 — job id unavailable after submission is never untracked
# ---------------------------------------------------------------------
async def test_job_id_missing_after_submit_is_recovered_by_unique_pipeline_name(flink_db, monkeypatch):
    fake = FakeFlinkClient(submit_mode="no_job_id")
    patch_flink_client(monkeypatch, fake)
    job_id = await _make_job(flink_db, status="deploying")
    deployment_id = await _make_deployment(flink_db, job_id)

    await _execute_deploy(deployment_id)

    dep = await _get(flink_db, FlinkDeployment, deployment_id)
    assert dep.flink_job_id == "job-1"
    assert dep.status == "running"
    assert (await _get(flink_db, FlinkJob, job_id)).status == "running"
    assert fake.list_jobs_called is False


async def test_job_id_missing_and_jobmanager_unreachable_leaves_it_for_the_reconciler(flink_db, monkeypatch):
    fake = FakeFlinkClient(submit_mode="no_job_id")
    fake.find_raises = True
    patch_flink_client(monkeypatch, fake)
    job_id = await _make_job(flink_db, status="deploying")
    deployment_id = await _make_deployment(flink_db, job_id)

    await _execute_deploy(deployment_id)

    dep = await _get(flink_db, FlinkDeployment, deployment_id)
    assert dep.status == "submitting" and dep.flink_job_id is None
    assert dep.submission_tag and dep.gateway_operation_handle == "op-1"
    assert (await _get(flink_db, FlinkJob, job_id)).status == "deploying"
    assert "reconciler" in dep.error_message

    # JobManager back: the reconciler re-attaches without resubmitting.
    fake.find_raises = False
    fake.submit_mode = "ok"
    await deployment_reconciler.reconcile_once()
    assert fake.execute_statements_calls == 1
    dep = await _get(flink_db, FlinkDeployment, deployment_id)
    assert dep.flink_job_id == "job-1" and dep.status == "running"


async def test_failed_submission_cancels_an_orphan_job_flink_did_start(flink_db, monkeypatch):
    fake = FakeFlinkClient()
    patch_flink_client(monkeypatch, fake)
    job_id = await _make_job(flink_db, status="deploying")
    deployment_id = await _make_deployment(flink_db, job_id)

    async def _boom(session_id, sql, on_submit=None):
        await FakeFlinkClient.execute_statements(fake, session_id, sql, on_submit=None)
        await on_submit("op-1", "INSERT")
        raise TimeoutError("status poll timed out")
    monkeypatch.setattr(fake, "execute_statements", _boom)

    await _execute_deploy(deployment_id)

    assert fake.cancelled == ["job-1"]
    assert (await _get(flink_db, FlinkDeployment, deployment_id)).status == "failed"
    assert (await _get(flink_db, FlinkJob, job_id)).status == "failed"


# ---------------------------------------------------------------------
# Crash after Gateway submit -> reattach, never resubmit
# ---------------------------------------------------------------------
async def test_crash_after_gateway_submit_is_reattached_not_resubmitted(flink_db, monkeypatch):
    fake = FakeFlinkClient(submit_mode="crash_after_submit")
    patch_flink_client(monkeypatch, fake)
    job_id = await _make_job(flink_db, status="validated")

    async with flink_db() as db:
        deployment = await JobService(db).deploy("org1", job_id, "u", DeployRequest())
        await db.commit()
    # Let the dispatch task run into the simulated crash.
    for _ in range(100):
        if fake.execute_statements_calls:
            break
        await leader_tick()
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.3)

    dep = await _get(flink_db, FlinkDeployment, deployment.id)
    # Persisted BEFORE the crash: everything needed to find the job again.
    assert dep.flink_job_id is None and dep.status == "submitting"
    assert dep.submission_tag and dep.gateway_operation_handle == "op-1"
    assert dep.submit_started_at is not None

    # The crashed owner's lease expires.
    async with flink_db() as db:
        await db.execute(update(FlinkDeployment).where(FlinkDeployment.id == dep.id)
                         .values(owner_id="dead-pod", lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
        await db.commit()

    fake.submit_mode = "ok"
    await deployment_reconciler.reconcile_once()

    assert fake.execute_statements_calls == 1            # never resubmitted
    dep = await _get(flink_db, FlinkDeployment, deployment.id)
    assert dep.flink_job_id == "job-1" and dep.status == "running"
    assert (await _get(flink_db, FlinkJob, job_id)).status == "running"


async def test_crash_with_jobmanager_down_waits_instead_of_resubmitting(flink_db, monkeypatch):
    fake = FakeFlinkClient()
    patch_flink_client(monkeypatch, fake)
    job_id = await _make_job(flink_db, status="deploying")
    deployment_id = await _make_deployment(
        flink_db, job_id, submission_tag="inaiera-tag-x", gateway_session_id="sess-1",
        gateway_operation_handle="op-9", submit_started_at=datetime.now(timezone.utc))
    fake.unknown = True

    await deployment_reconciler.reconcile_once()
    assert fake.execute_statements_calls == 0
    assert (await _get(flink_db, FlinkJob, job_id)).status == "deploying"


async def test_submission_absent_but_gateway_operation_still_running_waits(flink_db, monkeypatch):
    fake = FakeFlinkClient()
    fake.op_status = "RUNNING"
    patch_flink_client(monkeypatch, fake)
    job_id = await _make_job(flink_db, status="deploying")
    await _make_deployment(flink_db, job_id, submission_tag="inaiera-tag-y", gateway_session_id="s",
                           gateway_operation_handle="op-1", submit_started_at=datetime.now(timezone.utc))
    await deployment_reconciler.reconcile_once()
    assert fake.execute_statements_calls == 0

    fake.op_status = None   # gateway forgot it (restarted) and Flink has no such job -> safe to resubmit
    await deployment_reconciler.reconcile_once()
    assert fake.execute_statements_calls == 1
    await _wait_for(flink_db, job_id, "running")


# ---------------------------------------------------------------------
# reconciler — 'deploying'
# ---------------------------------------------------------------------
async def test_reconcile_deploying_resumes_a_deployment_that_never_reached_the_gateway(flink_db, monkeypatch):
    fake = FakeFlinkClient()
    patch_flink_client(monkeypatch, fake)
    job_id = await _make_job(flink_db, status="deploying")
    await _make_deployment(flink_db, job_id)

    await deployment_reconciler._reconcile_deploying()

    assert fake.execute_statements_calls == 1
    assert (await _get(flink_db, FlinkJob, job_id)).status == "running"


async def test_reconcile_deploying_reattaches_when_recorded_flink_job_is_alive(flink_db, monkeypatch):
    fake = FakeFlinkClient(job_states={"already-running-job": "RUNNING"})
    patch_flink_client(monkeypatch, fake)
    job_id = await _make_job(flink_db, status="deploying")
    await _make_deployment(flink_db, job_id, flink_job_id="already-running-job")

    await deployment_reconciler._reconcile_deploying()

    assert fake.execute_statements_calls == 0
    assert (await _get(flink_db, FlinkJob, job_id)).status == "running"


async def test_reconcile_deploying_resubmits_when_the_recorded_job_is_gone(flink_db, monkeypatch):
    fake = FakeFlinkClient(job_states={"stale-job-id": "FAILED"})
    patch_flink_client(monkeypatch, fake)
    job_id = await _make_job(flink_db, status="deploying")
    await _make_deployment(flink_db, job_id, flink_job_id="stale-job-id")

    await deployment_reconciler._reconcile_deploying()

    assert fake.execute_statements_calls == 1
    assert (await _get(flink_db, FlinkJob, job_id)).status == "running"


async def test_reconcile_deploying_does_not_resubmit_when_flink_state_unknown(flink_db, monkeypatch):
    fake = FakeFlinkClient()
    fake.unknown = True
    patch_flink_client(monkeypatch, fake)
    job_id = await _make_job(flink_db, status="deploying")
    await _make_deployment(flink_db, job_id, flink_job_id="maybe-alive")

    await deployment_reconciler._reconcile_deploying()
    assert fake.execute_statements_calls == 0
    assert (await _get(flink_db, FlinkJob, job_id)).status == "deploying"


async def test_reconcile_skips_a_deployment_with_a_live_task_in_this_process(flink_db, monkeypatch):
    fake = FakeFlinkClient()
    patch_flink_client(monkeypatch, fake)
    job_id = await _make_job(flink_db, status="deploying")
    deployment_id = await _make_deployment(flink_db, job_id)

    _ACTIVE_DEPLOYMENT_IDS.add(deployment_id)
    try:
        await deployment_reconciler._reconcile_deploying()
    finally:
        _ACTIVE_DEPLOYMENT_IDS.discard(deployment_id)
    assert fake.execute_statements_calls == 0


async def test_reconcile_skips_a_deployment_whose_lease_another_replica_holds(flink_db, monkeypatch):
    fake = FakeFlinkClient()
    patch_flink_client(monkeypatch, fake)
    job_id = await _make_job(flink_db, status="deploying")
    deployment_id = await _make_deployment(
        flink_db, job_id, owner_id="other-replica",
        lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=60))

    await deployment_reconciler._reconcile_deploying()
    assert fake.execute_statements_calls == 0

    # Lease expires (owner died) -> re-claimed and resumed.
    async with flink_db() as db:
        await db.execute(update(FlinkDeployment).where(FlinkDeployment.id == deployment_id)
                         .values(lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
        await db.commit()
    await deployment_reconciler._reconcile_deploying()
    assert fake.execute_statements_calls == 1


async def test_orphan_submitting_row_is_failed_so_it_cannot_block_future_deploys(flink_db, monkeypatch):
    patch_flink_client(monkeypatch, FakeFlinkClient())
    job_id = await _make_job(flink_db, status="failed")
    deployment_id = await _make_deployment(flink_db, job_id)
    await deployment_reconciler.reconcile_once()
    assert (await _get(flink_db, FlinkDeployment, deployment_id)).status == "failed"


# ---------------------------------------------------------------------
# reconciler — 'pausing'
# ---------------------------------------------------------------------
async def test_reconcile_pausing_retries_the_stop_when_flink_job_still_running(flink_db, monkeypatch):
    fake = FakeFlinkClient(job_states={"live-job": "RUNNING"})
    patch_flink_client(monkeypatch, fake)
    job_id = await _make_job(flink_db, status="pausing")
    await _make_deployment(flink_db, job_id, flink_job_id="live-job", stop_savepoint_dir="s3://ckpt",
                           status="running")

    await deployment_reconciler._reconcile_pausing()

    assert fake.stop_with_savepoint_calls == 1
    assert (await _get(flink_db, FlinkJob, job_id)).status == "paused"


async def test_transient_flink_outage_while_pausing_never_marks_paused(flink_db, monkeypatch):
    """The exact RUN-FLINK-013 bug: _flink_job_state returned None on a
    REST error and the reconciler treated it as 'gone' -> paused."""
    fake = FakeFlinkClient(job_states={"live-job": "RUNNING"})
    fake.unknown = True
    patch_flink_client(monkeypatch, fake)
    job_id = await _make_job(flink_db, status="pausing")
    deployment_id = await _make_deployment(flink_db, job_id, flink_job_id="live-job",
                                           stop_savepoint_dir="s3://ckpt", status="running")

    for _ in range(3):
        await deployment_reconciler._reconcile_pausing()
    assert (await _get(flink_db, FlinkJob, job_id)).status == "pausing"
    assert (await _get(flink_db, FlinkDeployment, deployment_id)).status == "running"
    assert fake.stop_with_savepoint_calls == 0

    fake.unknown = False   # outage over -> stop retried and completes
    await deployment_reconciler._reconcile_pausing()
    assert (await _get(flink_db, FlinkJob, job_id)).status == "paused"


async def test_reconcile_pausing_job_gone_with_completed_savepoint_is_paused(flink_db, monkeypatch):
    fake = FakeFlinkClient(job_states={"gone-job": "FINISHED"})
    fake.savepoint_status = {"status": "COMPLETED", "location": "s3://ckpt/sp-7"}
    patch_flink_client(monkeypatch, fake)
    job_id = await _make_job(flink_db, status="pausing")
    await _make_deployment(flink_db, job_id, flink_job_id="gone-job", stop_savepoint_dir="s3://ckpt",
                           status="running", stop_trigger_id="trig-7")

    await deployment_reconciler._reconcile_pausing()
    job = await _get(flink_db, FlinkJob, job_id)
    assert job.status == "paused" and job.savepoint_path == "s3://ckpt/sp-7"
    assert fake.stop_with_savepoint_calls == 0


async def test_reconcile_pausing_job_gone_without_confirmed_savepoint_is_not_called_paused(flink_db, monkeypatch):
    fake = FakeFlinkClient(job_states={})  # NOT_FOUND
    patch_flink_client(monkeypatch, fake)
    job_id = await _make_job(flink_db, status="pausing")
    deployment_id = await _make_deployment(flink_db, job_id, flink_job_id="gone-job",
                                           stop_savepoint_dir="s3://ckpt", status="running")

    await deployment_reconciler._reconcile_pausing()

    assert fake.stop_with_savepoint_calls == 0
    job = await _get(flink_db, FlinkJob, job_id)
    dep = await _get(flink_db, FlinkDeployment, deployment_id)
    assert job.status == "cancelled" and dep.status == "cancelled"
    assert "without a confirmed savepoint" in dep.error_message


# ---------------------------------------------------------------------
# _execute_stop failure leaves a consistent state
# ---------------------------------------------------------------------
@pytest.mark.parametrize("mode,job_status,dep_status", [
    ("fail_job_still_running", "running", "running"),
    ("fail_unknown", "pausing", "running"),
    ("fail_job_failed", "failed", "failed"),
])
async def test_failed_stop_leaves_job_and_deployment_consistent(flink_db, monkeypatch, mode, job_status, dep_status):
    fake = FakeFlinkClient(job_states={"live": "RUNNING"}, stop_mode=mode)
    patch_flink_client(monkeypatch, fake)
    job_id = await _make_job(flink_db, status="pausing")
    deployment_id = await _make_deployment(flink_db, job_id, flink_job_id="live",
                                           stop_savepoint_dir="s3://ckpt", status="running")

    await _execute_stop(deployment_id)

    job = await _get(flink_db, FlinkJob, job_id)
    dep = await _get(flink_db, FlinkDeployment, deployment_id)
    assert (job.status, dep.status) == (job_status, dep_status)
    assert dep.error_message


# ---------------------------------------------------------------------
# Leader election — the reconciler runs on exactly one replica
# ---------------------------------------------------------------------
async def test_reconciler_leader_lock_is_held_for_the_loop_lifetime(flink_db):
    a = LeaderElector(deployment_reconciler.LEADER_LOCK_NAME)
    b = LeaderElector(deployment_reconciler.LEADER_LOCK_NAME)
    try:
        assert await a.ensure() is True
        # Held across ticks, not per tick — B never gets in while A lives.
        for _ in range(3):
            assert await b.ensure() is False
            assert await a.ensure() is True
        await a.release()
        assert await b.ensure() is True
        assert await a.ensure() is False
    finally:
        await a.release()
        await b.release()


async def test_reconciler_leadership_fails_over_when_the_leader_connection_dies(flink_db):
    a = LeaderElector(deployment_reconciler.LEADER_LOCK_NAME)
    b = LeaderElector(deployment_reconciler.LEADER_LOCK_NAME)
    try:
        assert await a.ensure()
        await a._conn.invalidate()          # the leader pod/connection dies
        got = False
        for _ in range(50):
            if await b.ensure():
                got = True
                break
            await asyncio.sleep(0.1)
        assert got, "follower never took over after the leader's connection died"
        assert await a.ensure() is False    # the old leader does not come back as a second leader
    finally:
        await a.release()
        await b.release()


async def test_only_the_leader_replica_reconciles(flink_db, monkeypatch):
    fake = FakeFlinkClient()
    patch_flink_client(monkeypatch, fake)
    job_id = await _make_job(flink_db, status="deploying")
    await _make_deployment(flink_db, job_id)

    a = LeaderElector(deployment_reconciler.LEADER_LOCK_NAME)
    b = LeaderElector(deployment_reconciler.LEADER_LOCK_NAME)
    try:
        ran = await asyncio.gather(deployment_reconciler.tick_if_leader(a),
                                   deployment_reconciler.tick_if_leader(b))
        assert sorted(ran) == [False, True]
        assert fake.execute_statements_calls == 1
    finally:
        await a.release()
        await b.release()


# --- running deployments whose Flink job ended or vanished -----------------

async def test_running_job_missing_from_flink_is_marked_failed_with_reason(flink_db, monkeypatch):
    """After a Flink cluster restart the JobManager answers 404 for the job:
    the row used to stay 'running' forever (and the metrics poller failed on
    it every tick). NOT_FOUND is definitive, so it is marked failed."""
    fake = FakeFlinkClient(job_states={})            # cluster no longer knows it
    patch_flink_client(monkeypatch, fake)
    job_id = await _make_job(flink_db, status="running")
    dep_id = await _make_deployment(flink_db, job_id, flink_job_id="lost-job", status="running")

    await deployment_reconciler._reconcile_running()

    job, dep = await _get(flink_db, FlinkJob, job_id), await _get(flink_db, FlinkDeployment, dep_id)
    assert (job.status, dep.status) == ("failed", "failed")
    assert "no longer exists on the cluster" in dep.error_message
    assert dep.stopped_at is not None


async def test_running_job_is_untouched_while_jobmanager_is_unreachable(flink_db, monkeypatch):
    fake = FakeFlinkClient(job_states={"live-job": "RUNNING"})
    fake.unknown = True                              # transient outage: UNKNOWN
    patch_flink_client(monkeypatch, fake)
    job_id = await _make_job(flink_db, status="running")
    dep_id = await _make_deployment(flink_db, job_id, flink_job_id="live-job", status="running")

    for _ in range(3):
        await deployment_reconciler._reconcile_running()
    assert (await _get(flink_db, FlinkJob, job_id)).status == "running"
    assert (await _get(flink_db, FlinkDeployment, dep_id)).status == "running"


async def test_running_job_still_alive_is_left_running(flink_db, monkeypatch):
    patch_flink_client(monkeypatch, FakeFlinkClient(job_states={"live-job": "RUNNING"}))
    job_id = await _make_job(flink_db, status="running")
    await _make_deployment(flink_db, job_id, flink_job_id="live-job", status="running")
    await deployment_reconciler._reconcile_running()
    assert (await _get(flink_db, FlinkJob, job_id)).status == "running"


@pytest.mark.parametrize("flink_state,expected", [
    ("FINISHED", ("completed", "completed")),
    ("CANCELED", ("cancelled", "cancelled")),
    ("FAILED", ("failed", "failed")),
])
async def test_running_job_that_ended_in_flink_is_reflected(flink_db, monkeypatch, flink_state, expected):
    patch_flink_client(monkeypatch, FakeFlinkClient(job_states={"j": flink_state}))
    job_id = await _make_job(flink_db, status="running")
    dep_id = await _make_deployment(flink_db, job_id, flink_job_id="j", status="running")
    await deployment_reconciler._reconcile_running()
    job, dep = await _get(flink_db, FlinkJob, job_id), await _get(flink_db, FlinkDeployment, dep_id)
    assert (dep.status, job.status) == expected
