"""SQL jobs: a registered project of kind "sql" is one .sql file of a repository, run statement by statement against the engine.

It shares everything else with a dbt project — registration, the frozen manifest, dispatch, schedules, Dependents, Dagster — so
the tests below cover what is new: the registration rules, what the manifest records, and execution (real Postgres: the
throwaway *_test database stands in for pg_lake), including the all-or-nothing transaction and the Kubernetes Job arguments.
"""
from __future__ import annotations

import uuid
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text

from app.core.config import get_settings
from app.executors import isolation
from app.executors.sql_job_executor import run_sql_file, split_statements
from app.main import app
from app.models import Project, Run
from app.services import dependents, run_manifest
from app.services.run_service import _resolve_job_file, dispatch_run, freeze_project
from tests.test_isolation import no_real_k8s  # noqa: F401  (autouse: never a real kube config)


@pytest.fixture
async def api(run_db):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
def pg_lake(migrated_test_database, monkeypatch):
    """Point the runner's pg_lake engine at the throwaway *_test database (asyncpg URL -> plain postgres DSN)."""
    dsn = migrated_test_database.replace("postgresql+asyncpg://", "postgresql://")
    monkeypatch.setattr(get_settings(), "pg_lake_dsn", dsn)
    return dsn


def _table() -> str:
    return f"sqljob_{uuid.uuid4().hex[:10]}"


async def _query(run_db, sql: str):
    async with run_db() as db:
        return (await db.execute(text(sql))).all()


async def _drop(run_db, table: str) -> None:
    async with run_db() as db:
        await db.execute(text(f"drop table if exists {table}"))
        await db.commit()


# ── splitting a file into statements ─────────────────────────────────────────────────────────────────────────────────
def test_statements_are_split_in_order_without_comments_or_blanks():
    sql = """
    -- daily totals
    create table t (a int);
    /* seed */ insert into t values (1), (2);
    insert into t values (3) ; ;
    select 'a;b' as not_a_split;
    -- trailing comment only
    """
    assert split_statements(sql) == [
        "-- daily totals\n    create table t (a int)", "/* seed */ insert into t values (1), (2)",
        "insert into t values (3)", "select 'a;b' as not_a_split"]


# ── execution against Postgres ───────────────────────────────────────────────────────────────────────────────────────
async def test_a_sql_file_runs_every_statement_and_commits(pg_lake, run_db, tmp_path):
    t = _table()
    f = tmp_path / "job.sql"
    f.write_text(f"create table {t} (id int);\ninsert into {t} values (1), (2), (3);\nupdate {t} set id = id + 10 where id > 1;\n")
    try:
        result = run_sql_file(engine="pg_lake", sql_path=f)
        assert result["success"] is True and result["errors"] == []
        assert [(n["node_id"], n["status"], n["message"]) for n in result["nodes"]] == [
            ("statement.1", "success", "CREATE"), ("statement.2", "success", "INSERT · 3 rows"),
            ("statement.3", "success", "UPDATE · 2 rows")]
        assert sorted(r[0] for r in await _query(run_db, f"select id from {t}")) == [1, 12, 13]
    finally:
        await _drop(run_db, t)


async def test_a_failing_statement_rolls_everything_back_and_skips_the_rest(pg_lake, run_db, tmp_path):
    t = _table()
    f = tmp_path / "job.sql"
    f.write_text(f"create table {t} (id int);\ninsert into {t} values (1);\ninsert into no_such_table values (1);\n"
                 f"insert into {t} values (2);\n")
    result = run_sql_file(engine="pg_lake", sql_path=f)
    assert result["success"] is False
    assert [n["status"] for n in result["nodes"]] == ["success", "success", "error", "skipped"]
    assert 'relation "no_such_table" does not exist' in result["nodes"][2]["message"]
    assert result["errors"][0].startswith("statement 3 failed, nothing was committed:")
    assert await _query(run_db, f"select to_regclass('public.{t}')") == [(None,)]     # the CREATE was rolled back too


def test_an_empty_file_or_an_unconfigured_engine_is_a_failure_with_a_reason(tmp_path, monkeypatch):
    f = tmp_path / "job.sql"
    f.write_text("-- nothing here\n")
    assert run_sql_file(engine="pg_lake", sql_path=f)["errors"] == ["the SQL file has no statements"]
    f.write_text("select 1;")
    monkeypatch.setattr(get_settings(), "trino_host", None)
    assert "Trino engine is not configured" in run_sql_file(engine="trino", sql_path=f)["errors"][0]


def test_the_file_must_be_inside_the_repository(tmp_path):
    (tmp_path / "jobs").mkdir()
    (tmp_path / "jobs" / "a.sql").write_text("select 1")
    assert _resolve_job_file(tmp_path, "jobs/a.sql") == (tmp_path / "jobs" / "a.sql").resolve()
    with pytest.raises(RuntimeError, match="does not exist"):
        _resolve_job_file(tmp_path, "jobs/missing.sql")
    (tmp_path / "jobs" / "link.sql").symlink_to("/etc/hostname")
    with pytest.raises(RuntimeError, match="outside the repository"):
        _resolve_job_file(tmp_path, "jobs/link.sql")


# ── registration ─────────────────────────────────────────────────────────────────────────────────────────────────────
def _body(**over):
    return {"git_url": f"https://github.com/acme/jobs-{uuid.uuid4().hex[:6]}.git", "git_ref": "main", "engine": "pg_lake", **over}


async def test_a_sql_job_is_registered_with_its_file_and_listed_by_kind(api, auth_headers):
    r = await api.post("/api/v1/projects/register", headers=auth_headers, json=_body(kind="sql", project_path="jobs/daily.sql"))
    assert r.status_code == 201, r.text
    assert r.json()["kind"] == "sql" and r.json()["project_path"] == "jobs/daily.sql"
    dbt = await api.post("/api/v1/projects/register", headers=auth_headers, json=_body(project_path="customers"))
    assert dbt.json()["kind"] == "dbt"
    async def ids(kind):
        return {p["id"] for p in (await api.get(f"/api/v1/projects?kind={kind}", headers=auth_headers)).json()}
    assert await ids("sql") == {r.json()["id"]} and await ids("dbt") == {dbt.json()["id"]}


@pytest.mark.parametrize("body", [{"kind": "sql", "project_path": "jobs/"}, {"kind": "sql"},
                                  {"kind": "dbt", "project_path": "jobs/daily.sql"}, {"kind": "ruby", "project_path": "a.rb"}])
async def test_a_sql_job_needs_a_sql_file_and_a_dbt_project_cannot_be_one(api, auth_headers, body):
    r = await api.post("/api/v1/projects/register", headers=auth_headers, json=_body(**body))
    assert r.status_code == 422, r.text


async def test_registration_checks_the_file_is_in_the_repository(api, auth_headers, monkeypatch):
    monkeypatch.setattr(get_settings(), "validate_project_on_register", True)

    class Git:
        def file_exists(self, *, git_url, git_ref, path, token=None):
            return path == "jobs/daily.sql"
    monkeypatch.setattr("app.api.projects.get_git_service", lambda: Git())
    ok = await api.post("/api/v1/projects/register", headers=auth_headers, json=_body(kind="sql", project_path="jobs/daily.sql"))
    assert ok.status_code == 201, ok.text
    missing = await api.post("/api/v1/projects/register", headers=auth_headers, json=_body(kind="sql", project_path="jobs/nope.sql"))
    assert missing.status_code == 400 and missing.json()["detail"]["details"]["field"] == "project_path"
    assert "was not found in the repository" in missing.json()["detail"]["message"]


# ── the frozen manifest ──────────────────────────────────────────────────────────────────────────────────────────────
async def _project(run_db, kind="sql", path="jobs/daily.sql", name="jobs/daily") -> uuid.UUID:
    async with run_db() as db:
        p = Project(org_id=get_settings().org_id, name=name, kind=kind, project_path=path, engine="pg_lake",
                    git_url=f"https://github.com/acme/jobs-{uuid.uuid4().hex[:6]}.git", git_ref="main",
                    dq_schema_tests={"m": "version: 2"} if kind == "dbt" else None)
        db.add(p)
        await db.commit()
        return p.id


async def test_a_sql_runs_manifest_says_what_it_is_and_a_dbt_runs_keeps_its_shape(run_db):
    sql_id, dbt_id = await _project(run_db), await _project(run_db, kind="dbt", path="customers", name="c")
    async with run_db() as db:
        sql_m = await freeze_project(db, sql_id, None)
        dbt_m = await freeze_project(db, dbt_id, None)
    assert run_manifest.job_kind(sql_m) == "sql" and sql_m["project_path"] == "jobs/daily.sql"
    assert "kind" not in dbt_m and run_manifest.job_kind(dbt_m) == "dbt"


# ── dispatch: the real run path ──────────────────────────────────────────────────────────────────────────────────────
def _fake_git(monkeypatch, repo: Path):
    from tests.conftest import FAKE_COMMIT_SHA

    class Git:
        def clone_for_run(self, *, git_url, git_ref, run_id, sha=None, token=None):
            return repo

        def head_sha(self, path):
            return FAKE_COMMIT_SHA

        def tree_sha(self, clone_dir, project_dir):
            return "b" * 40

        def cleanup(self, run_id):
            pass
    monkeypatch.setattr("app.services.run_service.get_git_service", lambda: Git())
    monkeypatch.setattr("app.executors.job_entrypoint.get_git_service", lambda: Git())


async def test_a_scheduled_sql_job_runs_end_to_end_and_records_its_statements(pg_lake, run_db, tmp_path, monkeypatch):
    t = _table()
    (tmp_path / "jobs").mkdir()
    (tmp_path / "jobs" / "daily.sql").write_text(f"create table {t} (id int);\ninsert into {t} values (7);\n")
    _fake_git(monkeypatch, tmp_path)
    pid = await _project(run_db)
    async with run_db() as db:
        run = Run(project_id=pid, trigger="scheduled", status="queued", attempt=0)
        db.add(run)
        await db.commit()
    try:
        await dispatch_run(run.id)
        async with run_db() as db:
            done = await db.get(Run, run.id)
        assert done.status == "succeeded" and done.models_total == 2 and done.models_succeeded == 2
        assert done.manifest["kind"] == "sql" and done.source_tree_sha == "b" * 40
        assert await _query(run_db, f"select id from {t}") == [(7,)]
    finally:
        await _drop(run_db, t)


async def test_a_failed_sql_job_fails_its_own_run_and_leaves_the_jobs_below_to_dagster(pg_lake, run_db, tmp_path, monkeypatch):
    (tmp_path / "jobs").mkdir()
    (tmp_path / "jobs" / "daily.sql").write_text("insert into no_such_table values (1);\n")
    _fake_git(monkeypatch, tmp_path)
    sql_id = await _project(run_db)
    dbt_id = await _project(run_db, kind="dbt", path="reports", name="reports")
    async with run_db() as db:
        await dependents.set_dependents(db, await db.get(Project, sql_id), [dbt_id])
        job_run = Run(project_id=sql_id, trigger="scheduled", status="queued", attempt=0)
        waiting = Run(project_id=dbt_id, trigger="scheduled", status="queued", attempt=0)
        db.add_all([job_run, waiting])
        await db.commit()
    await dispatch_run(job_run.id)
    async with run_db() as db:
        failed, stopped = await db.get(Run, job_run.id), await db.get(Run, waiting.id)
    assert failed.status == "failed" and "statement 1 failed" in failed.error_message
    assert stopped.status == "queued"      # the rule for the jobs below is Dagster's (dependencies.py), not the Runner's


# ── Kubernetes: the isolated Job is told what to run ─────────────────────────────────────────────────────────────────
def test_the_kubernetes_job_is_started_as_a_sql_job(monkeypatch):
    from tests.test_isolation import _k8s_settings

    settings = _k8s_settings(environment="dev", artifact_store_backend="none", pg_lake_dsn="postgresql://x@h/db")
    sql = isolation._build_job(run_id=uuid.uuid4(), attempt=1, engine="pg_lake", select=None, settings=settings, kind="sql")
    dbt = isolation._build_job(run_id=uuid.uuid4(), attempt=1, engine="pg_lake", select=None, settings=settings)
    assert sql.spec.template.spec.containers[0].args[-2:] == ["--kind", "sql"]
    assert "--kind" not in dbt.spec.template.spec.containers[0].args


def test_the_job_entrypoint_runs_the_sql_file_instead_of_dbt(tmp_path, monkeypatch, capsys):
    from app.executors import job_entrypoint
    from tests.conftest import FAKE_COMMIT_SHA

    (tmp_path / "jobs").mkdir()
    (tmp_path / "jobs" / "daily.sql").write_text("select 1;")
    _fake_git(monkeypatch, tmp_path)
    seen = {}

    def fake_run_sql_file(*, engine, sql_path):
        seen.update(engine=engine, path=sql_path)
        return {"success": True, "errors": [], "nodes": [{"node_id": "statement.1", "status": "success"}]}
    monkeypatch.setattr("app.executors.sql_job_executor.run_sql_file", fake_run_sql_file)
    monkeypatch.setattr(job_entrypoint, "get_dbt_executor", lambda: pytest.fail("dbt must not run for a SQL job"))
    monkeypatch.setenv("RUNNER_GIT_URL", "https://github.com/acme/jobs.git")
    monkeypatch.setenv("RUNNER_GIT_REF", "main")
    monkeypatch.setenv("RUNNER_GIT_SHA", FAKE_COMMIT_SHA)
    monkeypatch.setenv("RUNNER_PROJECT_PATH", "jobs/daily.sql")
    monkeypatch.setattr(get_settings(), "artifact_store_backend", "none")
    assert job_entrypoint.main(["--run-id", str(uuid.uuid4()), "--engine", "pg_lake", "--kind", "sql"]) == 0
    assert seen == {"engine": "pg_lake", "path": (tmp_path / "jobs" / "daily.sql").resolve()}
    assert '"statement.1"' in capsys.readouterr().out


async def test_a_sql_run_in_kubernetes_mode_creates_a_sql_job(monkeypatch):
    """Through the public execute_run, as dispatch calls it (the kind must reach the Job the cluster runs)."""
    import json as _json

    from tests.conftest import FAKE_COMMIT_SHA
    from tests.test_isolation import FakeK8s, FakeLease, _inline_log, _k8s_settings, _use

    fake = FakeK8s(statuses=[(1, None)], pod_log=_inline_log({"success": True, "errors": [], "nodes": []}))
    _use(monkeypatch, fake, _k8s_settings())
    built = []
    real_build = isolation._build_job

    def spy(**kw):
        job = real_build(**kw)
        built.append(job.spec.template.spec.containers[0].args)
        return job
    monkeypatch.setattr(isolation, "_build_job", spy)
    run_id = uuid.uuid4()
    await isolation.RunnerIsolation().execute_run(
        run_id=run_id, project_dir=None, engine="pg_lake", select=None, git_url="https://github.com/acme/jobs.git",
        git_ref="main", project_id=uuid.uuid4(), lease=FakeLease(), project_path="jobs/daily.sql", git_sha=FAKE_COMMIT_SHA,
        dq_schema_blocks={}, kind="sql")
    assert len(built) == 1 and built[0][-2:] == ["--kind", "sql"]
    assert _json.loads(fake.secrets[isolation._secret_name(run_id, 1)].string_data["DQ_SCHEMA_TESTS_JSON"]) == {}
