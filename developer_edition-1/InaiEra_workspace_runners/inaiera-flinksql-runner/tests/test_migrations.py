"""Alembic migrations against a SEPARATE throwaway database
(<TEST_DATABASE_URL db>_mig_test on the same throwaway server — the name
still ends in _test and passes the same guard).

  * from scratch: base -> head, and the result matches the ORM models;
  * from 0005 (the revision the live runner DB is at) -> head, with data
    present: nothing lost, generations backfilled, metric columns widened;
  * 0005's / 0006's de-duplication of pre-existing duplicate 'submitting'
    rows, so the one-active-generation unique index can be created;
  * head -> 0005 -> head round trip.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from tests.conftest import _test_database_url_or_skip, alembic_config, assert_safe_test_database_url
from app.db.migration_url import sync_migration_url


@pytest.fixture
def mig_db():
    base_url = _test_database_url_or_skip()
    u = make_url(base_url)
    name = f"{u.database}_mig_{uuid.uuid4().hex[:6]}_test"
    url = assert_safe_test_database_url(u.set(database=name).render_as_string(hide_password=False))
    admin = create_engine(u.set(drivername="postgresql+psycopg2").render_as_string(hide_password=False),
                          isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        yield url
    finally:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def _sync(url: str):
    return create_engine(sync_migration_url(url), future=True)


def test_migrations_apply_from_scratch_and_match_the_models(mig_db):
    from alembic import command
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from app.db.base import Base
    import app.models  # noqa: F401

    command.upgrade(alembic_config(mig_db), "head")
    eng = _sync(mig_db)
    try:
        with eng.connect() as conn:
            assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == \
                "0010_quality_job_dq_artifacts"
            diffs = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    finally:
        eng.dispose()
    # Column comments exist only in 0002 (informational) — not a schema mismatch.
    diffs = [d for d in diffs if not (isinstance(d, list) and d and d[0][0] == "modify_comment")]
    assert diffs == [], f"models and migrations disagree: {diffs}"


def test_upgrade_from_0005_with_live_like_data_is_lossless(mig_db):
    from alembic import command

    cfg = alembic_config(mig_db)
    command.upgrade(cfg, "0005_deploy_stop_idempotency")
    eng = _sync(mig_db)
    try:
        with eng.begin() as conn:
            job_ids = []
            for i in range(3):
                jid = conn.execute(text(
                    "INSERT INTO flink_jobs (org_id, name, sql, status, savepoint_path) "
                    "VALUES ('org1', :n, 'INSERT INTO t SELECT 1', :st, :sp) RETURNING id"),
                    {"n": f"job-{i}", "st": ["running", "paused", "failed"][i],
                     "sp": "s3://sp/1" if i == 1 else None}).scalar()
                job_ids.append(jid)
                for k, status in enumerate(["failed", "running" if i == 0 else "paused"]):
                    conn.execute(text(
                        "INSERT INTO flink_deployments (job_id, status, flink_job_id, created_at) "
                        "VALUES (:j, :s, :f, now() - make_interval(mins => :m))"),
                        {"j": jid, "s": status, "f": f"fj-{i}-{k}", "m": 10 - k})
            conn.execute(text(
                "INSERT INTO flink_metric_snapshots (job_id, kafka_consumer_lag, heap_used_bytes, "
                "kafka_lag_detail) VALUES (:j, 5, 1000, '{\"t:0\": 5}')"), {"j": job_ids[0]})

        command.upgrade(cfg, "head")

        with eng.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM flink_jobs")).scalar() == 3
            assert conn.execute(text("SELECT count(*) FROM flink_deployments")).scalar() == 6
            rows = conn.execute(text(
                "SELECT j.name, d.generation, d.flink_job_id, d.consumer_groups::text, d.owner_id "
                "FROM flink_deployments d JOIN flink_jobs j ON j.id = d.job_id "
                "ORDER BY j.name, d.generation")).all()
            assert [(r[0], r[1]) for r in rows] == [
                ("job-0", 1), ("job-0", 2), ("job-1", 1), ("job-1", 2), ("job-2", 1), ("job-2", 2)]
            assert rows[1][2] == "fj-0-1"                    # live flink job ids preserved
            assert all(r[3] == "[]" and r[4] is None for r in rows)
            counters = dict(conn.execute(text("SELECT name, generation_counter FROM flink_jobs")).all())
            assert counters == {"job-0": 2, "job-1": 2, "job-2": 2}
            assert conn.execute(text(
                "SELECT savepoint_path FROM flink_jobs WHERE name = 'job-1'")).scalar() == "s3://sp/1"
            assert conn.execute(text(
                "SELECT data_type FROM information_schema.columns WHERE table_name = "
                "'flink_metric_snapshots' AND column_name = 'heap_used_bytes'")).scalar() == "bigint"
            conn.execute(text("SELECT 1 FROM kafka_preview_audit LIMIT 1"))
            conn.execute(text("SELECT 1 FROM flink_savepoints LIMIT 1"))
    finally:
        eng.dispose()


def test_0005_dedupes_duplicate_submitting_rows_before_creating_the_unique_index(mig_db):
    from alembic import command

    cfg = alembic_config(mig_db)
    command.upgrade(cfg, "0004_deployment_desired_state")
    eng = _sync(mig_db)
    try:
        with eng.begin() as conn:
            jid = conn.execute(text(
                "INSERT INTO flink_jobs (org_id, name, sql, status) "
                "VALUES ('org1', 'dup', 'INSERT INTO t SELECT 1', 'deploying') RETURNING id")).scalar()
            for m in (3, 2, 1):
                conn.execute(text(
                    "INSERT INTO flink_deployments (job_id, status, created_at) "
                    "VALUES (:j, 'submitting', now() - make_interval(mins => :m))"), {"j": jid, "m": m})

        command.upgrade(cfg, "head")   # would fail building the unique index without the dedupe

        with eng.connect() as conn:
            statuses = conn.execute(text(
                "SELECT status, error_message FROM flink_deployments ORDER BY created_at")).all()
        assert [s for s, _ in statuses] == ["failed", "failed", "submitting"]   # newest kept
        assert all("de-duplicated" in (e or "") for s, e in statuses if s == "failed")
    finally:
        eng.dispose()


def test_0006_is_safe_on_a_db_that_already_ran_0005(mig_db):
    """The live DB is at 0005 (index already exists). 0006 re-runs the
    dedupe and asserts the index with IF NOT EXISTS — no error."""
    from alembic import command

    cfg = alembic_config(mig_db)
    command.upgrade(cfg, "0005_deploy_stop_idempotency")
    command.upgrade(cfg, "head")
    eng = _sync(mig_db)
    try:
        with eng.connect() as conn:
            n = conn.execute(text(
                "SELECT count(*) FROM pg_indexes WHERE indexname = 'uq_flink_dep_active_generation'")).scalar()
        assert n == 1
    finally:
        eng.dispose()


def test_downgrade_to_0005_and_back_up(mig_db):
    from alembic import command

    cfg = alembic_config(mig_db)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0005_deploy_stop_idempotency")
    command.upgrade(cfg, "head")


def test_execution_manifest_migration_roundtrip_and_immutability_trigger(mig_db):
    """X-004 (0008): the columns and the trigger go away on downgrade and come back on upgrade; at head the trigger
    refuses to change a deployment's manifest, its hash or its sql_hash once set, and allows every other update."""
    import pytest
    from alembic import command
    from sqlalchemy.exc import DBAPIError

    cfg = alembic_config(mig_db)
    command.upgrade(cfg, "head")
    eng = _sync(mig_db)
    try:
        with eng.begin() as conn:
            conn.execute(text("INSERT INTO flink_jobs (id, org_id, name, sql, parallelism, sink_engine, job_kind, status, "
                              "validation_errors, generation_counter) VALUES ('00000000-0000-0000-0000-0000000000a1', 'o', 'j', "
                              "'select 1', 1, 'trino', 'transform', 'draft', '{}', 0)"))
            conn.execute(text("INSERT INTO flink_deployments (id, job_id, status, trigger, parallelism, stop_drain, consumer_groups, "
                              "sql_hash, manifest, manifest_sha256) VALUES ('00000000-0000-0000-0000-0000000000b1', "
                              "'00000000-0000-0000-0000-0000000000a1', 'submitting', 'manual', 1, true, '[]'::jsonb, 'h', "
                              "'{\"a\": 1}'::jsonb, 'x')"))
        for stmt in ("UPDATE flink_deployments SET manifest = '{\"a\": 2}'::jsonb",
                     "UPDATE flink_deployments SET manifest_sha256 = 'y'",
                     "UPDATE flink_deployments SET sql_hash = 'other'",
                     "UPDATE flink_deployments SET manifest = NULL"):
            with pytest.raises(DBAPIError, match="immutable"), eng.begin() as conn:
                conn.execute(text(stmt))
        with eng.begin() as conn:                                  # everything else still updates
            conn.execute(text("UPDATE flink_deployments SET status = 'running', error_message = 'x'"))
    finally:
        eng.dispose()
    command.downgrade(cfg, "0007_quality_runs_lifecycle")
    eng = _sync(mig_db)
    try:
        with eng.connect() as conn:
            cols = {r[0] for r in conn.execute(text(
                "SELECT column_name FROM information_schema.columns WHERE table_name IN ('flink_deployments', 'flink_jobs')"))}
            trg = conn.execute(text("SELECT count(*) FROM pg_trigger WHERE tgname = 'trg_flink_deployments_manifest_immutable'")).scalar()
        assert not ({"manifest", "manifest_sha256", "source"} & cols) and trg == 0
    finally:
        eng.dispose()
    command.upgrade(cfg, "head")


def test_outbox_backoff_migration_keeps_existing_rows_due_and_downgrades_safely(mig_db):
    """FL-004/005 (0009): rows from before it stay deliverable (due at once), and a downgrade turns the two new statuses
    back into 'pending' instead of failing on the old CHECK."""
    import pytest
    from alembic import command
    from sqlalchemy.exc import DBAPIError

    cfg = alembic_config(mig_db)
    command.upgrade(cfg, "0008_execution_manifest")
    eng = _sync(mig_db)
    try:
        with eng.begin() as conn:
            conn.execute(text("INSERT INTO quality_result_outbox (idempotency_key, kind, payload, status) "
                              "VALUES ('old-1', 'quality_run', '{}'::jsonb, 'pending'), "
                              "('old-2', 'quality_run', '{}'::jsonb, 'delivered')"))
        command.upgrade(cfg, "head")
        with eng.connect() as conn:
            rows = conn.execute(text("SELECT status, next_attempt_at <= now(), lease_owner FROM quality_result_outbox "
                                     "ORDER BY idempotency_key")).all()
        assert [tuple(r) for r in rows] == [("pending", True, None), ("delivered", True, None)]
        with eng.begin() as conn:                                  # the new statuses are allowed at head ...
            conn.execute(text("UPDATE quality_result_outbox SET status = 'parked' WHERE idempotency_key = 'old-1'"))
        with pytest.raises(DBAPIError), eng.begin() as conn:       # ... and nothing else is
            conn.execute(text("UPDATE quality_result_outbox SET status = 'bogus'"))
        command.downgrade(cfg, "0008_execution_manifest")
        with eng.connect() as conn:
            assert conn.execute(text("SELECT status FROM quality_result_outbox WHERE idempotency_key = 'old-1'")).scalar() \
                == "pending"
            cols = {r[0] for r in conn.execute(text(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'quality_result_outbox'"))}
        assert not ({"next_attempt_at", "lease_owner", "lease_expires_at"} & cols)
    finally:
        eng.dispose()
    command.upgrade(cfg, "head")
