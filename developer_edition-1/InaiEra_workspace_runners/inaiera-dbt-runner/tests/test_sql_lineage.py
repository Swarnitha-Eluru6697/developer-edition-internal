"""A SQL job is a box in the DATA lineage too: its statements say which tables it reads and writes.

dbt reports its lineage in a manifest; a SQL job has none, so after a successful run its own statements are read (`statement_lineage`):
the schema-qualified tables a statement reads (after FROM / JOIN) paired with the table it creates or changes. Those become "sql" edges
and the existing matching (data_dependencies.data_upstream) links the job below the project that builds a table it reads, and a project
below the job when it reads a table the job writes. Catalogs are dropped (`postgresql.public.dim_customers` is `public.dim_customers`).
A failed run records nothing; a Python job is never read.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.executors import connection_sql_job, sql_job_executor
from app.models import LineageEdge, Project, Run
from app.services.data_dependencies import data_upstream, statement_lineage
from app.services.run_service import _finalize_run, dispatch_run
from tests.test_data_dependencies import _build
from tests.test_data_dependencies import _project as _dbt_project
from tests.test_sql_jobs import _drop, _fake_git, _table, pg_lake  # noqa: F401  (pg_lake: fixture)


# ── reading the statements ───────────────────────────────────────────────────────────────────────────────────────────────
def test_a_create_table_as_select_reads_its_sources_and_writes_its_target():
    assert statement_lineage(["create table reports.summary as select segment, count(*) from public.dim_customers group by 1"]) == [
        ("public.dim_customers", "reports.summary")]


def test_every_read_is_paired_with_the_write_including_joins_and_subqueries():
    sql = ("insert into reports.snap select (select count(*) from public.dim_customers), count(*) "
           "from public.fct_orders o join public.dim_products p on p.id = o.product_id where o.is_revenue")
    assert statement_lineage([sql]) == [("public.dim_customers", "reports.snap"), ("public.fct_orders", "reports.snap"),
                                        ("public.dim_products", "reports.snap")]


def test_a_catalog_is_dropped_so_trino_and_postgres_names_meet():
    assert statement_lineage(["insert into iceberg.reports.snap select * from postgresql.public.dim_customers"]) == [
        ("public.dim_customers", "reports.snap")]


@pytest.mark.parametrize("statement,write", [
    ("create or replace view reports.v as select * from public.a", "reports.v"),
    ("create table if not exists reports.t as select * from public.a", "reports.t"),
    ("create temporary table reports.t as select * from public.a", "reports.t"),
    ("create materialized view reports.m as select * from public.a", "reports.m"),
    ("merge into reports.t using (select * from public.a) s on true when matched then update set x = 1", "reports.t"),
    ("update reports.t set x = 1 from public.a where reports.t.id = public.a.id", "reports.t"),
    ("INSERT INTO Reports.T SELECT * FROM Public.A", "reports.t"),
    ('insert into "reports"."t" select * from "public"."a"', "reports.t"),
])
def test_the_ways_a_statement_can_write(statement, write):
    assert statement_lineage([statement]) == [("public.a", write)]


def test_delete_reads_nothing_it_writes():
    assert statement_lineage(["delete from reports.t where id in (select id from public.a)"]) == [("public.a", "reports.t")]


def test_a_table_a_statement_both_reads_and_writes_is_not_its_own_source():
    assert statement_lineage(["insert into reports.t select * from reports.t union all select * from public.a"]) == [("public.a", "reports.t")]


def test_what_is_not_a_read_or_a_write_is_ignored():
    assert statement_lineage([
        "create schema if not exists reports",
        "drop table if exists reports.t",
        "select * from public.dim_customers",                              # a read with no target
        "create table reports.x (id int)",                                 # a write with no source
        "insert into reports.x values (1)",
        "truncate table reports.x",
    ]) == []


def test_comments_strings_and_cte_names_are_not_tables():
    sql = """
        -- insert into reports.ghost select * from public.ghost
        with recent as (select * from public.a /* join public.hidden */)
        insert into reports.t select r.*, 'from public.other' as note from recent r
    """
    assert statement_lineage([sql]) == [("public.a", "reports.t")]


def test_a_name_without_a_schema_is_not_guessed():
    assert statement_lineage(["create table loose as select * from public.a", "insert into reports.t select * from other"]) == []


def test_edges_from_several_statements_come_in_order_without_repeats():
    assert statement_lineage([
        "create table reports.a as select * from public.x",
        "create table reports.b as select * from reports.a",
        "insert into reports.a select * from public.x",
    ]) == [("public.x", "reports.a"), ("reports.a", "reports.b")]


# ── a SQL job's result carries them ──────────────────────────────────────────────────────────────────────────────────────
def _sql_file(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "job.sql"
    p.write_text(text)
    return p


def test_a_successful_sql_job_reports_its_lineage(tmp_path, monkeypatch):
    monkeypatch.setattr(sql_job_executor, "_run_postgres", lambda statements, settings: {"success": True, "errors": [], "nodes": []})
    monkeypatch.setattr(sql_job_executor, "_run_trino", lambda statements, settings: {"success": True, "errors": [], "nodes": []})
    for engine in ("pg_lake", "trino"):
        result = sql_job_executor.run_sql_file(engine=engine, sql_path=_sql_file(tmp_path, "create table reports.t as select * from public.a;"))
        assert result["lineage"] == [("public.a", "reports.t")], engine


def test_a_failed_sql_job_reports_no_lineage(tmp_path, monkeypatch):
    monkeypatch.setattr(sql_job_executor, "_run_postgres", lambda statements, settings: {"success": False, "errors": ["boom"], "nodes": []})
    assert "lineage" not in sql_job_executor.run_sql_file(engine="pg_lake", sql_path=_sql_file(tmp_path, "create table reports.t as select * from public.a;"))


def test_a_sql_job_on_a_connection_reports_its_lineage_too(tmp_path, monkeypatch):
    class Resp:
        def __init__(self, ok):
            self.status_code, self._ok = 200, ok

        def json(self):
            return {"success": self._ok, "errors": [] if self._ok else ["boom"], "nodes": []}

    settings = get_settings()
    monkeypatch.setattr(settings, "workspace_url", "http://workspace.test")
    path = _sql_file(tmp_path, "insert into reports.t select * from public.a;")
    monkeypatch.setattr(connection_sql_job.httpx, "post", lambda *a, **k: Resp(True))
    assert connection_sql_job.run_sql_file_on_connection(sql_path=path, token="t")["lineage"] == [("public.a", "reports.t")]
    monkeypatch.setattr(connection_sql_job.httpx, "post", lambda *a, **k: Resp(False))
    assert "lineage" not in connection_sql_job.run_sql_file_on_connection(sql_path=path, token="t")


# ── stored with the run, and matched to the project that builds the table ────────────────────────────────────────────────
async def _sql_project(run_db, name="reports") -> uuid.UUID:
    async with run_db() as db:
        p = Project(org_id=get_settings().org_id, name=f"inaiera-dbt-test/{name}", kind="sql", project_path=f"jobs/{name}.sql", engine="pg_lake",
                    git_url=f"https://github.com/acme/{name}-{uuid.uuid4().hex[:6]}.git", git_ref="main")
        db.add(p)
        await db.commit()
        return p.id


async def _finish(run_db, project_id, result) -> uuid.UUID:
    async with run_db() as db:
        run = Run(project_id=project_id, trigger="manual", status="running", attempt=1, started_at=datetime.now(UTC))
        db.add(run)
        await db.commit()
    assert await _finalize_run(run.id, project_id, None, result)
    return run.id


async def _edges(run_db, project_id):
    async with run_db() as db:
        return [(e.source_table, e.target_table, e.relationship_type)
                for e in (await db.execute(select(LineageEdge).where(LineageEdge.project_id == project_id))).scalars()]


async def _upstream(run_db):
    async with run_db() as db:
        return await data_upstream(db, get_settings().org_id)


OK = {"success": True, "errors": [], "nodes": []}


async def test_a_successful_run_stores_its_edges_as_sql_edges(run_db):
    job = await _sql_project(run_db)
    await _finish(run_db, job, {**OK, "lineage": [["public.dim_customers", "reports.summary"]]})   # JSON turns tuples into lists
    assert await _edges(run_db, job) == [("public.dim_customers", "reports.summary", "sql")]


async def test_a_failed_run_stores_nothing(run_db):
    job = await _sql_project(run_db)
    await _finish(run_db, job, {"success": False, "errors": ["boom"], "nodes": [], "lineage": [["public.a", "reports.b"]]})
    assert await _edges(run_db, job) == []


async def test_a_sql_job_reading_a_table_a_dbt_project_builds_is_below_that_project(run_db):
    customers, job = await _dbt_project(run_db, "customers"), await _sql_project(run_db)
    await _build(run_db, customers, [('"dbtrunner"."public"."raw"', '"dbtrunner"."public"."dim_customers"')])
    await _finish(run_db, job, {**OK, "lineage": [["public.dim_customers", "reports.summary"]]})
    assert await _upstream(run_db) == {job: [customers]}


async def test_a_dbt_project_reading_a_table_a_sql_job_writes_is_below_the_job(run_db):
    job, report = await _sql_project(run_db), await _dbt_project(run_db, "report")
    await _finish(run_db, job, {**OK, "lineage": [["public.dim_customers", "reports.summary"]]})
    await _build(run_db, report, [('"dbtrunner"."reports"."summary"', '"dbtrunner"."public"."exec_view"')])
    assert await _upstream(run_db) == {report: [job]}


async def test_a_chain_of_dbt_then_sql_then_dbt(run_db):
    customers, job, report = await _dbt_project(run_db, "customers"), await _sql_project(run_db), await _dbt_project(run_db, "report")
    await _build(run_db, customers, [("raw.c", '"dbtrunner"."public"."dim_customers"')])
    await _finish(run_db, job, {**OK, "lineage": [["public.dim_customers", "reports.summary"]]})
    await _build(run_db, report, [('"dbtrunner"."reports"."summary"', '"dbtrunner"."public"."exec_view"')])
    assert await _upstream(run_db) == {job: [customers], report: [job]}


async def test_a_table_two_projects_build_is_not_guessed_for_a_sql_job_either(run_db):
    a, b, job = await _dbt_project(run_db, "a"), await _dbt_project(run_db, "b"), await _sql_project(run_db)
    await _build(run_db, a, [("raw.x", "public.shared")])
    await _build(run_db, b, [("raw.y", "public.shared")])
    await _finish(run_db, job, {**OK, "lineage": [["public.shared", "reports.s"]]})
    assert job not in await _upstream(run_db)


async def test_only_the_latest_successful_run_counts_and_a_later_failure_changes_nothing(run_db):
    customers, job = await _dbt_project(run_db, "customers"), await _sql_project(run_db)
    await _build(run_db, customers, [("raw.c", "public.dim_customers")])
    await _finish(run_db, job, {**OK, "lineage": [["public.dim_customers", "reports.summary"]]})
    await _finish(run_db, job, {"success": False, "errors": ["boom"], "nodes": [], "lineage": []})
    assert await _upstream(run_db) == {job: [customers]}
    await _finish(run_db, job, {**OK, "lineage": [["public.unknown_table", "reports.summary"]]})     # the new run reads something else
    assert job not in await _upstream(run_db)


# ── the real run path ────────────────────────────────────────────────────────────────────────────────────────────────────
async def test_a_scheduled_sql_job_records_what_it_read_and_wrote(pg_lake, run_db, tmp_path, monkeypatch):
    source, target = _table(), _table()
    (tmp_path / "jobs").mkdir()
    (tmp_path / "jobs" / "daily.sql").write_text(
        f"create table public.{source} (id int);\ninsert into public.{source} values (1);\n"
        f"create table public.{target} as select id from public.{source};\n")
    _fake_git(monkeypatch, tmp_path)
    async with run_db() as db:
        p = Project(org_id=get_settings().org_id, name="jobs/daily", kind="sql", project_path="jobs/daily.sql", engine="pg_lake",
                    git_url=f"https://github.com/acme/jobs-{uuid.uuid4().hex[:6]}.git", git_ref="main")
        db.add(p)
        await db.flush()
        run = Run(project_id=p.id, trigger="scheduled", status="queued", attempt=0)
        db.add(run)
        await db.commit()
    try:
        await dispatch_run(run.id)
        assert await _edges(run_db, p.id) == [(f"public.{source}", f"public.{target}", "sql")]
        builder = await _dbt_project(run_db, "builder")
        await _build(run_db, builder, [("raw.x", f'"dbtrunner"."public"."{source}"')])
        assert await _upstream(run_db) == {p.id: [builder]}
    finally:
        await _drop(run_db, f"public.{target}")
        await _drop(run_db, f"public.{source}")
