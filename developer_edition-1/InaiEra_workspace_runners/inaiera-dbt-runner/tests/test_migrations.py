"""Alembic migrations are exercised for real (the review noted they never
were): the whole suite's schema comes from `upgrade head` (conftest), and
this module checks the migrated schema matches the models and that the
newest migrations downgrade/upgrade cleanly. Only ever runs against the
guarded *_test database."""
from __future__ import annotations

import asyncio

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

import app.models
from app.db.base import Base
from tests.conftest import _alembic_config


def _diff(url: str):
    async def _go():
        eng = create_async_engine(url)
        async with eng.connect() as conn:
            def _cmp(sync_conn):
                ctx = MigrationContext.configure(sync_conn, opts={"compare_type": True})
                return compare_metadata(ctx, Base.metadata)
            diff = await conn.run_sync(_cmp)
        await eng.dispose()
        return diff
    return asyncio.run(_go())


def _columns(url: str, table: str) -> set[str]:
    async def _go():
        eng = create_async_engine(url)
        async with eng.connect() as conn:
            cols = await conn.run_sync(lambda c: {col["name"] for col in inspect(c).get_columns(table)})
        await eng.dispose()
        return cols
    return asyncio.run(_go())


def test_migrated_schema_matches_models(migrated_test_database):
    assert _diff(migrated_test_database) == []


def test_full_downgrade_to_base_and_back_to_head(migrated_test_database):
    cfg = _alembic_config(migrated_test_database)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    assert _diff(migrated_test_database) == []


def test_downgrade_and_upgrade_roundtrip(migrated_test_database):
    cfg = _alembic_config(migrated_test_database)
    command.downgrade(cfg, "0004_metadata_outbox")
    assert "lease_owner" not in _columns(migrated_test_database, "runs")
    command.upgrade(cfg, "head")
    assert {"lease_owner", "lease_expires_at", "attempt", "k8s_job_name", "job_released_at"} <= \
        _columns(migrated_test_database, "runs")
    assert _diff(migrated_test_database) == []


def _run(url: str, work):
    """Run `work(session)` against the migrated database on a throwaway engine."""
    async def _go():
        eng = create_async_engine(url)
        try:
            async with AsyncSession(eng) as db:
                return await work(db)
        finally:
            await eng.dispose()
    return asyncio.run(_go())


def test_cancelling_status_migration_roundtrip(migrated_test_database):
    """DBT-005 (0008): `cancelling` is accepted at head, rejected by the older schema, and a run still cancelling
    when the migration is rolled back reads `cancelled` (the older schema cannot say "in progress")."""
    url = migrated_test_database

    async def seed(db):
        project = app.models.Project(org_id="o", name="p", git_url="https://example.test/r.git")
        db.add(project)
        await db.flush()
        run = app.models.Run(project_id=project.id, status="running")
        db.add(run)
        await db.flush()
        run_id = run.id
        await db.commit()
        return run_id

    run_id = _run(url, seed)

    async def set_status(db):
        await db.execute(text("UPDATE runs SET status='cancelling', cancel_requested_at=now() WHERE id=:i"),
                         {"i": run_id})
        await db.commit()
    _run(url, set_status)                          # head accepts it

    cfg = _alembic_config(url)
    command.downgrade(cfg, "0007_project_path")
    assert "cancel_requested_at" not in _columns(url, "runs")

    async def read(db):
        return (await db.execute(text("SELECT status, completed_at FROM runs WHERE id=:i"), {"i": run_id})).one()
    status, completed_at = _run(url, read)
    assert status == "cancelled" and completed_at is not None

    async def reject(db):
        with pytest.raises(IntegrityError):
            await db.execute(text("UPDATE runs SET status='cancelling' WHERE id=:i"), {"i": run_id})
    _run(url, reject)                              # the older schema refuses it

    command.upgrade(cfg, "head")
    assert "cancel_requested_at" in _columns(url, "runs")
    assert _diff(url) == []


def test_run_manifest_migration_roundtrip_and_immutability_trigger(migrated_test_database):
    """DBT-006/007 (0009): the columns and the trigger go away on downgrade and come back on upgrade; at head the
    trigger refuses to change a manifest, its hash or the frozen commit, and allows every other update."""
    url = migrated_test_database
    cfg = _alembic_config(url)

    async def trigger_names(db):
        rows = await db.execute(text("SELECT tgname FROM pg_trigger WHERE tgrelid = 'runs'::regclass AND NOT tgisinternal"))
        return {r[0] for r in rows}

    assert "trg_runs_manifest_immutable" in _run(url, trigger_names)
    assert {"manifest", "manifest_sha256"} <= _columns(url, "runs")

    command.downgrade(cfg, "0008_run_cancelling")
    assert not {"manifest", "manifest_sha256"} & _columns(url, "runs")
    assert "trg_runs_manifest_immutable" not in _run(url, trigger_names)

    command.upgrade(cfg, "head")
    assert {"manifest", "manifest_sha256"} <= _columns(url, "runs")
    assert "trg_runs_manifest_immutable" in _run(url, trigger_names)
    assert _diff(url) == []


def test_run_idempotency_and_evidence_migration_roundtrip(migrated_test_database):
    """X-004 (0010): the idempotency columns, the unique (project, key) index and the evidence column come and go."""
    url = migrated_test_database
    cfg = _alembic_config(url)
    wanted = {"idempotency_key", "idempotency_request_hash", "source_tree_sha"}

    async def index_names(db):
        rows = await db.execute(text("SELECT indexname FROM pg_indexes WHERE tablename = 'runs'"))
        return {r[0] for r in rows}

    assert wanted <= _columns(url, "runs") and "uq_runs_idempotency_key" in _run(url, index_names)
    command.downgrade(cfg, "0009_run_manifest")
    assert not wanted & _columns(url, "runs") and "uq_runs_idempotency_key" not in _run(url, index_names)
    command.upgrade(cfg, "head")
    assert wanted <= _columns(url, "runs") and "uq_runs_idempotency_key" in _run(url, index_names)
    assert _diff(url) == []


# DBT-009: migration 0011 removes credentials from stored repository URLs (and refuses to create duplicates while doing it).
def _exec(url: str, sql: str, params: dict | None = None, *, fetch: bool = False):
    async def _go():
        eng = create_async_engine(url)
        async with eng.begin() as conn:
            res = await conn.execute(text(sql), params or {})
            rows = res.fetchall() if fetch else None
        await eng.dispose()
        return rows
    return asyncio.run(_go())


_INSERT = ("INSERT INTO projects (id, org_id, name, git_url, git_ref, project_path, engine) "
           "VALUES (gen_random_uuid(), 'o', :n, :u, :r, '', 'pg_lake')")


def test_migration_0011_scrubs_credentials_from_git_urls_and_keeps_ssh_user(migrated_test_database):
    url = migrated_test_database
    cfg = _alembic_config(url)
    command.downgrade(cfg, "0010_run_idempotency_evidence")
    try:
        _exec(url, "DELETE FROM projects")
        for n, u in [("a", "https://ci:ghp_SECRET@github.com/acme/a.git"), ("b", "https://ghp_SECRET2@github.com/acme/b.git"),
                     ("c", "https://github.com/acme/c.git"), ("d", "ssh://git@github.com/acme/d.git"),
                     ("e", "https://u:p@host:8443/acme/e.git#subdirectory=dbt")]:
            _exec(url, _INSERT, {"n": n, "u": u, "r": "main"})
        command.upgrade(cfg, "head")
        rows = dict(_exec(url, "SELECT name, git_url FROM projects", fetch=True))
        assert rows == {"a": "https://github.com/acme/a.git", "b": "https://github.com/acme/b.git", "c": "https://github.com/acme/c.git",
                        "d": "ssh://git@github.com/acme/d.git", "e": "https://host:8443/acme/e.git#subdirectory=dbt"}
        assert "credential_ref" in _columns(url, "projects")
    finally:
        _exec(url, "DELETE FROM projects")


def test_migration_0011_refuses_to_merge_two_projects_into_one(migrated_test_database):
    url = migrated_test_database
    cfg = _alembic_config(url)
    command.downgrade(cfg, "0010_run_idempotency_evidence")
    try:
        _exec(url, "DELETE FROM projects")
        _exec(url, _INSERT, {"n": "plain", "u": "https://github.com/acme/x.git", "r": "main"})
        _exec(url, _INSERT, {"n": "dirty", "u": "https://u:tok@github.com/acme/x.git", "r": "main"})
        with pytest.raises(Exception, match="become duplicates"):
            command.upgrade(cfg, "head")
    finally:
        _exec(url, "DELETE FROM projects")
    # leave the schema at head for the rest of the session once the offending row is gone
    command.upgrade(cfg, "head")
