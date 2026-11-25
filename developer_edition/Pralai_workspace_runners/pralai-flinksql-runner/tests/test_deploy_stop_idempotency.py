"""RUN-FLINK-014 — deploy()/stop() had a status-guarded UPDATE (RUN-FLINK-013)
that stops two concurrent calls from both winning, but nothing stopped a
client's own *retry* of a call that had already succeeded from either
erroring out (stop) or racing a second Flink submission (deploy), and
nothing but that application-level UPDATE actually enforced "one active
generation per job" — there was no real database uniqueness backing it.

These tests prove:
  * An idempotency key makes deploy()/stop() safe to retry: the same key
    always returns the exact deployment the first call produced, even
    after job.status has moved on, without a second Flink submission.
  * uq_flink_dep_active_generation is a REAL Postgres unique index, not
    just app logic — a second 'submitting' row for the same job is
    rejected by the database itself, independent of job_service's own
    checks.
  * A concurrent retry with the same idempotency key (the TOCTOU case: two
    requests both see "no existing row yet" and race to insert) is
    resolved to a single winning deployment, not a duplicate submission —
    exercising the SAVEPOINT recovery path in job_service.deploy()/stop().

Uses a real, schema-backed Postgres database (see test_deployment_reconciler.py
for why) with the same fake Flink Gateway/JobManager client.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models import FlinkDeployment, FlinkJob
from app.schemas import DeployRequest, StopRequest
from app.services.job_service import JobService
from tests.fakes import FakeFlinkClient, leader_tick, patch_flink_client


@pytest.fixture
def flink_db(pg_db):
    """The throwaway, migrated test database (tests/conftest.py pg_db —
    TEST_DATABASE_URL only, refuses anything not named *_test)."""
    return pg_db


async def _make_job(flink_db, *, status: str, sql: str = "INSERT INTO t SELECT 1;") -> uuid.UUID:
    async with flink_db() as db:
        job = FlinkJob(org_id="org1", name=f"job-{uuid.uuid4().hex[:8]}", sql=sql, status=status)
        db.add(job)
        await db.commit()
        return job.id


async def _wait_for_job_status(flink_db, job_id: uuid.UUID, status: str, *, timeout: float = 5.0) -> None:
    """Poll instead of a fixed sleep — the dispatched deploy/stop runs as a
    fire-and-forget background task, and a flat sleep is prone to flaking
    under the load of the full suite (many tests sharing one CPU)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        async with flink_db() as db:
            job = await db.get(FlinkJob, job_id)
            if job.status == status:
                return
        await leader_tick()        # FL-011: the leader's loop is what starts the persisted deploy/stop
        await asyncio.sleep(0.02)
    raise AssertionError(f"job {job_id} never reached status={status!r} (last: {job.status!r})")


class _FakeFlinkClient(FakeFlinkClient):
    pass


def _patch_flink_client(monkeypatch, fake_client):
    patch_flink_client(monkeypatch, fake_client)


# ---------------------------------------------------------------------
# deploy() — idempotency key makes a retry safe
# ---------------------------------------------------------------------
async def test_deploy_retry_with_same_key_returns_the_original_deployment(flink_db, monkeypatch):
    fake_client = _FakeFlinkClient()
    _patch_flink_client(monkeypatch, fake_client)
    job_id = await _make_job(flink_db, status="validated")

    async with flink_db() as db:
        first = await JobService(db).deploy(
            "org1", job_id, "user1", DeployRequest(idempotency_key="client-key-1"),
        )
        await db.commit()
        first_id = first.id

    # The job has moved to 'deploying' by now — a naive retry would fail
    # the "must be validated/paused/failed" check. The idempotency key
    # must short-circuit straight to the original row instead.
    async with flink_db() as db:
        job = await db.get(FlinkJob, job_id)
        assert job.status == "deploying"

    async with flink_db() as db:
        retried = await JobService(db).deploy(
            "org1", job_id, "user1", DeployRequest(idempotency_key="client-key-1"),
        )
        assert retried.id == first_id

    await _wait_for_job_status(flink_db, job_id, "running")
    assert fake_client.execute_statements_calls == 1  # never submitted twice

    async with flink_db() as db:
        deployments = (await db.execute(
            select(FlinkDeployment).where(FlinkDeployment.job_id == job_id)
        )).scalars().all()
    assert len(deployments) == 1


async def test_deploy_retry_after_job_finished_running_still_returns_original(flink_db, monkeypatch):
    """The stronger case: the retry arrives after the deploy has already
    completed and the job is 'running' — not just mid-flight."""
    fake_client = _FakeFlinkClient()
    _patch_flink_client(monkeypatch, fake_client)
    job_id = await _make_job(flink_db, status="validated")

    async with flink_db() as db:
        first = await JobService(db).deploy(
            "org1", job_id, "user1", DeployRequest(idempotency_key="client-key-2"),
        )
        await db.commit()
        first_id = first.id

    await _wait_for_job_status(flink_db, job_id, "running")

    async with flink_db() as db:
        retried = await JobService(db).deploy(
            "org1", job_id, "user1", DeployRequest(idempotency_key="client-key-2"),
        )
        assert retried.id == first_id
    assert fake_client.execute_statements_calls == 1


async def test_deploy_without_idempotency_key_still_conflicts_on_concurrent_calls(flink_db, monkeypatch):
    """No key supplied — behavior unchanged from RUN-FLINK-013: distinct
    concurrent attempts for the same job still can't both win."""
    fake_client = _FakeFlinkClient()
    _patch_flink_client(monkeypatch, fake_client)
    job_id = await _make_job(flink_db, status="validated")

    async def _attempt():
        async with flink_db() as db:
            try:
                await JobService(db).deploy("org1", job_id, "user1", DeployRequest())
                await db.commit()
                return "won"
            except HTTPException:
                return "lost"

    results = await asyncio.gather(_attempt(), _attempt())
    assert sorted(results) == ["lost", "won"]
    await _wait_for_job_status(flink_db, job_id, "running")
    assert fake_client.execute_statements_calls == 1


async def test_concurrent_deploy_with_same_idempotency_key_produces_one_deployment(flink_db, monkeypatch):
    """The TOCTOU race this finding calls out: two requests carrying the
    SAME client-generated idempotency key both check "does this key
    exist yet?" at once, see nothing, and race to insert. The database's
    own unique index — not application logic — must be what stops both
    from succeeding; exactly one submission may reach Flink."""
    fake_client = _FakeFlinkClient()
    _patch_flink_client(monkeypatch, fake_client)
    job_id = await _make_job(flink_db, status="validated")

    async def _attempt():
        async with flink_db() as db:
            dep = await JobService(db).deploy(
                "org1", job_id, "user1", DeployRequest(idempotency_key="shared-key"),
            )
            await db.commit()
            return dep.id

    ids = await asyncio.gather(_attempt(), _attempt())
    assert ids[0] == ids[1]  # both callers resolved to the same deployment

    await _wait_for_job_status(flink_db, job_id, "running")
    assert fake_client.execute_statements_calls == 1

    async with flink_db() as db:
        deployments = (await db.execute(
            select(FlinkDeployment).where(
                FlinkDeployment.job_id == job_id,
                FlinkDeployment.idempotency_key == "shared-key",
            )
        )).scalars().all()
    assert len(deployments) == 1


# ---------------------------------------------------------------------
# stop() — idempotency key makes a retry safe
# ---------------------------------------------------------------------
async def test_stop_retry_after_job_already_paused_still_returns_original(flink_db, monkeypatch):
    fake_client = _FakeFlinkClient()
    _patch_flink_client(monkeypatch, fake_client)
    job_id = await _make_job(flink_db, status="validated")

    async with flink_db() as db:
        await JobService(db).deploy("org1", job_id, "user1", DeployRequest())
        await db.commit()
    await _wait_for_job_status(flink_db, job_id, "running")

    async with flink_db() as db:
        first = await JobService(db).stop(
            "org1", job_id, StopRequest(idempotency_key="stop-key-1"),
        )
        await db.commit()
        first_id = first.id
    await _wait_for_job_status(flink_db, job_id, "paused")

    # A naive retry here would hit "Job is not running" — the idempotency
    # key must resolve straight to the original stop instead.
    async with flink_db() as db:
        retried = await JobService(db).stop(
            "org1", job_id, StopRequest(idempotency_key="stop-key-1"),
        )
        assert retried.id == first_id
    assert fake_client.stop_with_savepoint_calls == 1


# ---------------------------------------------------------------------
# The DB uniqueness itself — independent of job_service's own checks
# ---------------------------------------------------------------------
async def test_active_generation_index_is_enforced_by_postgres_directly(flink_db):
    """Bypass job_service entirely: two 'submitting' rows for the same job,
    inserted straight through the ORM, must be rejected by the database's
    own uq_flink_dep_active_generation index — proving the guarantee does
    not depend on job_service ever calling the guarded UPDATE at all."""
    job_id = await _make_job(flink_db, status="validated")

    async with flink_db() as db:
        db.add(FlinkDeployment(job_id=job_id, trigger="manual"))  # status defaults to 'submitting'
        await db.commit()

    async with flink_db() as db:
        db.add(FlinkDeployment(job_id=job_id, trigger="manual"))
        with pytest.raises(IntegrityError):
            await db.commit()


# ---------------------------------------------------------------------
# RUN-FLINK-014 — key reuse with a different payload is rejected
# ---------------------------------------------------------------------
async def test_deploy_key_reused_with_a_different_payload_is_rejected(flink_db, monkeypatch):
    fake_client = _FakeFlinkClient()
    _patch_flink_client(monkeypatch, fake_client)
    job_id = await _make_job(flink_db, status="validated")

    async with flink_db() as db:
        await JobService(db).deploy("org1", job_id, "u", DeployRequest(idempotency_key="k", parallelism=2))
        await db.commit()

    async with flink_db() as db:
        with pytest.raises(HTTPException) as exc:
            await JobService(db).deploy("org1", job_id, "u", DeployRequest(idempotency_key="k", parallelism=3))
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "IDEMPOTENCY_KEY_REUSED"

    # The identical payload still replays the original.
    async with flink_db() as db:
        again = await JobService(db).deploy("org1", job_id, "u", DeployRequest(idempotency_key="k", parallelism=2))
        assert again.parallelism == 2
    await _wait_for_job_status(flink_db, job_id, "running")
    assert fake_client.execute_statements_calls == 1


async def test_stop_key_reused_with_a_different_payload_is_rejected(flink_db, monkeypatch):
    fake_client = _FakeFlinkClient()
    _patch_flink_client(monkeypatch, fake_client)
    job_id = await _make_job(flink_db, status="validated")
    async with flink_db() as db:
        await JobService(db).deploy("org1", job_id, "u", DeployRequest())
        await db.commit()
    await _wait_for_job_status(flink_db, job_id, "running")

    async with flink_db() as db:
        await JobService(db).stop("org1", job_id, StopRequest(idempotency_key="s", drain=True))
        await db.commit()
    await _wait_for_job_status(flink_db, job_id, "paused")

    async with flink_db() as db:
        with pytest.raises(HTTPException) as exc:
            await JobService(db).stop("org1", job_id, StopRequest(idempotency_key="s", drain=False))
    assert exc.value.status_code == 422


# ---------------------------------------------------------------------
# RUN-FLINK-014 — cancel() is guarded and idempotent
# ---------------------------------------------------------------------
async def _running_job(flink_db, monkeypatch, fake):
    _patch_flink_client(monkeypatch, fake)
    job_id = await _make_job(flink_db, status="validated")
    async with flink_db() as db:
        await JobService(db).deploy("org1", job_id, "u", DeployRequest())
        await db.commit()
    await _wait_for_job_status(flink_db, job_id, "running")
    return job_id


async def test_cancel_is_idempotent(flink_db, monkeypatch):
    fake = _FakeFlinkClient()
    job_id = await _running_job(flink_db, monkeypatch, fake)

    async with flink_db() as db:
        first = await JobService(db).cancel("org1", job_id)
        await db.commit()
    async with flink_db() as db:
        second = await JobService(db).cancel("org1", job_id)
        await db.commit()
    assert first.id == second.id and second.status == "cancelled"
    assert fake.cancelled == ["job-1"]   # Flink was asked exactly once


async def test_concurrent_cancels_cancel_once_and_both_return_the_same_deployment(flink_db, monkeypatch):
    fake = _FakeFlinkClient()
    job_id = await _running_job(flink_db, monkeypatch, fake)

    async def _cancel():
        async with flink_db() as db:
            dep = await JobService(db).cancel("org1", job_id)
            await db.commit()
            return dep.id

    ids = await asyncio.gather(_cancel(), _cancel())
    assert ids[0] == ids[1]
    assert fake.cancelled == ["job-1"]
    async with flink_db() as db:
        assert (await db.get(FlinkJob, job_id)).status == "cancelled"


async def test_cancel_that_flink_rejects_leaves_the_job_running(flink_db, monkeypatch):
    fake = _FakeFlinkClient()
    job_id = await _running_job(flink_db, monkeypatch, fake)
    fake.cancel_raises = True

    async with flink_db() as db:
        with pytest.raises(HTTPException) as exc:
            await JobService(db).cancel("org1", job_id)
        await db.rollback()
    assert exc.value.status_code == 502
    async with flink_db() as db:
        assert (await db.get(FlinkJob, job_id)).status == "running"


async def test_cancel_of_a_job_that_is_not_running_is_rejected(flink_db, monkeypatch):
    _patch_flink_client(monkeypatch, _FakeFlinkClient())
    job_id = await _make_job(flink_db, status="validated")
    async with flink_db() as db:
        with pytest.raises(HTTPException) as exc:
            await JobService(db).cancel("org1", job_id)
    assert exc.value.status_code == 422


async def test_cancel_during_pausing_wins_and_the_stop_does_not_overwrite_it(flink_db, monkeypatch):
    fake = _FakeFlinkClient()
    job_id = await _running_job(flink_db, monkeypatch, fake)
    # Freeze the stop task so the cancel lands while the job is 'pausing'.
    release = asyncio.Event()
    orig_stop = fake.stop_with_savepoint

    async def _slow_stop(*a, **kw):
        await release.wait()
        return await orig_stop(*a, **kw)
    monkeypatch.setattr(fake, "stop_with_savepoint", _slow_stop)

    async with flink_db() as db:
        await JobService(db).stop("org1", job_id, StopRequest())
        await db.commit()
    await _wait_for_job_status(flink_db, job_id, "pausing")
    await leader_tick()                     # the reconciler starts the (slow) stop
    await asyncio.sleep(0.2)
    async with flink_db() as db:
        await JobService(db).cancel("org1", job_id)
        await db.commit()
    release.set()
    await asyncio.sleep(0.5)
    async with flink_db() as db:
        assert (await db.get(FlinkJob, job_id)).status == "cancelled"
