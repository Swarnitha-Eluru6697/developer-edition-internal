"""RUN-FLINK-011 — job_service used to call client.list_jobs() (every job
in the whole Flink cluster) and heuristically guess which one was "ours"
by name prefix or by picking whichever happened to be RUNNING, falling
all the way back to jobs[0] if neither matched. Status, cancel, metrics
and savepoint calls all key off deployment.flink_job_id afterwards, so
binding to the wrong job there means every one of those operations can
silently target someone else's job.

_job_id_from_statement_results() (pure) is tested directly; the full
submission path (_execute_deploy() — renamed/restructured by RUN-FLINK-013
so it can also be resumed by the deployment reconciler, but the job-ID
capture behavior itself is unchanged) is tested against a real,
schema-backed Postgres database (the throwaway TEST_DATABASE_URL database — see
tests/conftest.py; never the local stack's live DB) rather than mocks, since the
point is proving the actual persisted deployment.flink_job_id is the one
the Gateway reported, and that client.list_jobs() is never even called on
the way there.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app.models import FlinkDeployment, FlinkJob
from app.services.job_service import _execute_deploy, _job_id_from_statement_results


# ---------------------------------------------------------------------
# _job_id_from_statement_results — pure
# ---------------------------------------------------------------------
def test_picks_the_one_statement_that_reported_a_job_id():
    results = [
        {"statement": "CREATE TABLE t ...", "flink_job_id": None},
        {"statement": "INSERT INTO t ...", "flink_job_id": "the-real-job-id"},
    ]
    assert _job_id_from_statement_results(results) == "the-real-job-id"


def test_returns_none_rather_than_guessing_when_nothing_reported_one():
    results = [{"statement": "CREATE TABLE t ...", "flink_job_id": None}]
    assert _job_id_from_statement_results(results) is None


def test_returns_none_for_an_empty_result_list():
    assert _job_id_from_statement_results([]) is None


def test_picks_the_insert_statements_job_id_not_merely_the_first_reported_value():
    """RUN-FLINK-011: a non-submitting statement's stray value must never be
    taken as the deployment's job id."""
    results = [
        {"statement": "SET 'x' = 'y'", "kind": "SET", "flink_job_id": "not-a-real-job"},
        {"statement": "CREATE TABLE t ...", "kind": "CREATE", "flink_job_id": "also-bogus"},
        {"statement": "INSERT INTO t ...", "kind": "INSERT", "flink_job_id": "the-insert-job"},
    ]
    assert _job_id_from_statement_results(results) == "the-insert-job"


def test_picks_the_statement_set_job_id():
    results = [
        {"statement": "SET 'a' = 'b'", "kind": "SET", "flink_job_id": "bogus"},
        {"statement": "EXECUTE STATEMENT SET BEGIN ...", "kind": "STATEMENT_SET",
         "flink_job_id": "set-job"},
    ]
    assert _job_id_from_statement_results(results) == "set-job"


def test_kind_is_derived_from_the_statement_text_when_absent():
    results = [
        {"statement": "-- comment\nSET 'a' = 'b'", "flink_job_id": "bogus"},
        {"statement": "/* c */ INSERT INTO t SELECT 1", "flink_job_id": "real"},
    ]
    assert _job_id_from_statement_results(results) == "real"


def test_two_distinct_submitted_jobs_is_an_error_not_a_guess():
    import pytest as _pytest
    results = [
        {"statement": "INSERT INTO a ...", "kind": "INSERT", "flink_job_id": "j1"},
        {"statement": "EXECUTE STATEMENT SET ...", "kind": "STATEMENT_SET", "flink_job_id": "j2"},
    ]
    with _pytest.raises(RuntimeError):
        _job_id_from_statement_results(results)


# ---------------------------------------------------------------------
# _submit_to_flink — real database, fake Gateway client
# ---------------------------------------------------------------------


@pytest.fixture
def flink_db(pg_db):
    """The throwaway, migrated test database (tests/conftest.py pg_db —
    TEST_DATABASE_URL only, refuses anything not named *_test)."""
    return pg_db


async def _make_job_and_deployment(flink_db) -> tuple[FlinkJob, FlinkDeployment]:
    async with flink_db() as db:
        job = FlinkJob(
            org_id="org1", name=f"job-{uuid.uuid4().hex[:8]}", sql="INSERT INTO t SELECT 1;",
            status="deploying",
        )
        db.add(job)
        await db.flush()
        deployment = FlinkDeployment(job_id=job.id, trigger="manual", generation=1,
                                     submitted_at=datetime.now(timezone.utc))
        db.add(deployment)
        await db.commit()
        return job, deployment


class _FakeFlinkClient:
    def __init__(self, statement_results, *, list_jobs_forbidden=True):
        self._statement_results = statement_results
        self._list_jobs_forbidden = list_jobs_forbidden
        self.list_jobs_called = False

    async def create_session(self, session_name=None):
        return "sess-1"

    async def execute_statements(self, session_id, sql, on_submit=None):
        if on_submit is not None:
            await on_submit("op-1", "INSERT ...")
        return self._statement_results

    async def find_jobs_by_name(self, name):
        return []   # nothing in the cluster carries this deployment's tag

    async def close_session(self, session_id):
        pass

    async def list_jobs(self):
        self.list_jobs_called = True
        if self._list_jobs_forbidden:
            raise AssertionError(
                "list_jobs() must never be called — job identity is no longer "
                "inferred by scanning the cluster (RUN-FLINK-011)"
            )
        return []


async def test_submit_to_flink_persists_the_gateway_reported_job_id_without_ever_listing_jobs(
    flink_db, monkeypatch,
):
    job, deployment = await _make_job_and_deployment(flink_db)
    fake_client = _FakeFlinkClient(statement_results=[
        {"statement": "INSERT INTO t ...", "kind": "INSERT", "flink_job_id": "gateway-reported-job-id"},
    ])
    monkeypatch.setattr("app.services.job_service.get_flink_client", lambda: fake_client)

    await _execute_deploy(deployment.id)

    async with flink_db() as db:
        persisted = await db.get(FlinkDeployment, deployment.id)
        persisted_job = await db.get(FlinkJob, job.id)

    assert persisted.flink_job_id == "gateway-reported-job-id"
    assert persisted.status == "running"
    assert persisted_job.status == "running"
    assert fake_client.list_jobs_called is False


async def test_submit_to_flink_fails_the_deployment_when_gateway_reports_no_job_id(
    flink_db, monkeypatch,
):
    """No result reported a job ID at all — this must be a clear failure,
    never a fallback guess at some other job in the cluster."""
    job, deployment = await _make_job_and_deployment(flink_db)
    fake_client = _FakeFlinkClient(statement_results=[
        {"statement": "CREATE TABLE t ...", "flink_job_id": None},
    ])
    monkeypatch.setattr("app.services.job_service.get_flink_client", lambda: fake_client)

    await _execute_deploy(deployment.id)

    async with flink_db() as db:
        persisted = await db.get(FlinkDeployment, deployment.id)
        persisted_job = await db.get(FlinkJob, job.id)

    assert persisted.status == "failed"
    assert persisted_job.status == "failed"
    assert persisted.flink_job_id is None
    assert "no job id" in persisted.error_message
    assert fake_client.list_jobs_called is False
