"""
BUG-06 against a REAL PostgreSQL: the retention sweep deletes expired input only for jobs that have FINISHED — never for a queued or
running one. The in-memory fake mirrors the rule but cannot prove the SQL, so this runs it for real.

    DATACONNECT_DATABASE_URL=postgresql+asyncpg://dataconnect:dataconnect@localhost:5435/dataconnect \\
    DATACONNECT_REAL_DB_TESTS=1 .venv/bin/python -m pytest tests/test_quality_job_records_real_db.py --no-cov

Uses an existing pipeline and quality policy of org_1001 for the foreign keys; creates jobs and chunks, and deletes them afterwards.
"""

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.skipif(not os.environ.get("DATACONNECT_REAL_DB_TESTS"), reason="needs a real PostgreSQL (DATACONNECT_REAL_DB_TESTS=1)")

ORG = "org_1001"


@pytest.fixture
async def db():
    from app.db.session import AsyncSessionLocal, engine

    await engine.dispose()
    created: list[str] = []
    async with AsyncSessionLocal() as session:
        pipeline = (await session.execute(text("select id from dc_pipelines where org_id=:o limit 1"), {"o": ORG})).scalar()
        policy = (await session.execute(text("select id from dc_quality_policies where org_id=:o limit 1"), {"o": ORG})).scalar()
    if not pipeline or not policy:
        pytest.skip("needs an existing org_1001 pipeline and quality policy")

    async def make_job(status: str, *, expired: bool = True) -> str:
        job_id = str(uuid.uuid4())
        when = datetime.now(UTC) - timedelta(days=3) if expired else datetime.now(UTC) + timedelta(days=1)
        async with AsyncSessionLocal() as session:
            await session.execute(text(
                "insert into dc_quality_execution_jobs (id, org_id, pipeline_id, quality_policy_id, status, record_count, chunk_count) "
                "values (:i, :o, :p, :q, :s, 1, 1)"), {"i": job_id, "o": ORG, "p": pipeline, "q": policy, "s": status})
            await session.execute(text(
                "insert into dc_quality_execution_job_records (id, job_id, org_id, chunk_index, byte_size, records, expires_at) "
                "values (:id, :j, :o, 0, 1, '[]'::jsonb, :e)"), {"id": str(uuid.uuid4()), "j": job_id, "o": ORG, "e": when})
            await session.commit()
        created.append(job_id)
        return job_id

    async def chunk_count(job_id: str) -> int:
        async with AsyncSessionLocal() as session:
            return (await session.execute(text("select count(*) from dc_quality_execution_job_records where job_id=:j"), {"j": job_id})).scalar()

    yield make_job, chunk_count
    async with AsyncSessionLocal() as session:
        for job_id in created:
            await session.execute(text("delete from dc_quality_execution_jobs where id=:i"), {"i": job_id})   # chunks cascade
        await session.commit()
    await engine.dispose()


async def test_the_sweep_keeps_expired_input_of_queued_and_running_jobs_and_removes_finished_ones(db) -> None:
    from app.services.quality_job_runner import cleanup_expired_records

    make_job, chunk_count = db
    queued, running = await make_job("queued"), await make_job("running")
    succeeded, failed, stale = await make_job("succeeded"), await make_job("failed"), await make_job("stale")
    fresh_failed = await make_job("failed", expired=False)

    await cleanup_expired_records()

    assert await chunk_count(queued) == 1                          # expired long ago, but the job still needs it
    assert await chunk_count(running) == 1
    assert await chunk_count(succeeded) == 0
    assert await chunk_count(failed) == 0
    assert await chunk_count(stale) == 0
    assert await chunk_count(fresh_failed) == 1                    # not expired yet


async def test_a_job_can_finish_and_then_be_swept_only_afterwards(db) -> None:
    from app.db.session import AsyncSessionLocal
    from app.services.quality_job_runner import cleanup_expired_records

    make_job, chunk_count = db
    job = await make_job("running")
    await cleanup_expired_records()
    assert await chunk_count(job) == 1

    async with AsyncSessionLocal() as session:
        await session.execute(text("update dc_quality_execution_jobs set status='succeeded' where id=:i"), {"i": job})
        await session.commit()
    await cleanup_expired_records()
    assert await chunk_count(job) == 0
