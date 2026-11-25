"""FL-001: pg_lake sinks use a governed catalog configured on the runner; SQL never carries a credential.

The old template wrote `${PG_LAKE_JDBC_URL}`, `${PG_USER}` and `${PG_PASSWORD}` into the user's SQL. Nothing resolved them, so
the advertised path reached validation and deployment with unresolved text; and substituting them would have put a database
password into stored, user-visible SQL (the job row, every deployment manifest, API responses). Now the runner registers a
named JDBC catalog from its own configuration (a Secret in the chart) in each Gateway session; the SQL only names it:
`INSERT INTO pg_lake.<database>.<table> ...`. Credentials written in SQL, and placeholders nothing substitutes, are refused.
"""
from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import select

from app.api.kafka_catalog import SQL_TEMPLATES
from app.core.config import Settings, get_settings
from app.models import FlinkDeployment, FlinkJob
from app.schemas import DeployRequest
from app.services import pg_lake
from app.services.job_service import JobService, _execute_deploy
from app.services.validate_service import validate_job_sql
from tests.fakes import FakeFlinkClient, patch_flink_client

PASSWORD = "S3cret-pw-9f2"
OLD_TEMPLATE_SQL = """CREATE TABLE sink (id INT) WITH (
  'connector' = 'jdbc', 'url' = '${PG_LAKE_JDBC_URL}', 'table-name' = 't', 'driver' = 'org.postgresql.Driver',
  'username' = '${PG_USER}', 'password' = '${PG_PASSWORD}');
INSERT INTO sink SELECT 1;"""
GOOD_SQL = "CREATE TEMPORARY TABLE src (id INT) WITH ('connector'='datagen');\nINSERT INTO pg_lake.analytics.orders_sink SELECT id FROM src;"


def configured(**over) -> Settings:
    base = {"pg_lake_jdbc_base_url": "jdbc:postgresql://pg:5432", "pg_lake_database": "analytics",
            "pg_lake_user": "lake_writer", "pg_lake_password": PASSWORD}
    base.update(over)
    return Settings(**base)


@pytest.fixture
def pg_settings(monkeypatch):
    s = get_settings()
    for k, v in configured().model_dump().items():
        if k.startswith("pg_lake_") and k != "pg_lake_catalog_name":
            monkeypatch.setattr(s, k, v)
    return s


@pytest.fixture
def unconfigured(monkeypatch):
    s = get_settings()
    for k in ("pg_lake_jdbc_base_url", "pg_lake_database", "pg_lake_user", "pg_lake_password"):
        monkeypatch.setattr(s, k, None)
    return s


# ---- the pure rules --------------------------------------------------------
def test_the_catalog_needs_all_four_settings():
    assert pg_lake.is_configured(configured())
    for missing in ("pg_lake_jdbc_base_url", "pg_lake_database", "pg_lake_user", "pg_lake_password"):
        assert not pg_lake.is_configured(configured(**{missing: None}))
        assert not pg_lake.is_configured(configured(**{missing: ""}))


def test_the_catalog_statement_is_built_from_runner_configuration():
    stmt = pg_lake.catalog_statement(configured())
    assert stmt.startswith("CREATE CATALOG `pg_lake` WITH (")
    for fragment in ("'type'='jdbc'", "'base-url'='jdbc:postgresql://pg:5432'", "'default-database'='analytics'",
                     "'username'='lake_writer'", f"'password'='{PASSWORD}'"):
        assert fragment in stmt


def test_a_quote_in_a_setting_cannot_break_out_of_the_statement():
    stmt = pg_lake.catalog_statement(configured(pg_lake_password="pa'ss"))
    assert "'password'='pa''ss'" in stmt


@pytest.mark.parametrize("sql,expected", [
    ("INSERT INTO pg_lake.analytics.t SELECT 1", True),
    ("INSERT INTO `pg_lake`.analytics.t SELECT 1", True),
    ("INSERT INTO PG_LAKE.analytics.t SELECT 1", True),
    ("SELECT * FROM x -- pg_lake.analytics.t", False),
    ("SELECT 'pg_lake.analytics.t'", False),
    ("INSERT INTO my_pg_lake.analytics.t SELECT 1", False),
    ("INSERT INTO other.pg_lake.t SELECT 1", False),
    ("INSERT INTO t SELECT 1", False),
])
def test_only_real_references_to_the_catalog_count(sql, expected):
    assert pg_lake.references_catalog(sql, configured()) is expected


@pytest.mark.parametrize("sql,names", [
    ("... 'username' = '${PG_USER}', 'password' = '${PG_PASSWORD}'", ["password"]),
    ("... 'password' = 'hunter2'", ["password"]),
    ("... 'fs.s3a.secret.key' = 'abc'", ["fs.s3a.secret.key"]),
    ("... 'properties.sasl.jaas.config' = 'org.x.PlainLoginModule required username=\"u\" password=\"pw\";'",
     ["(a password inside an option value)", "properties.sasl.jaas.config"]),      # FL-008: the JAAS option itself is a credential channel
    ("... 'avro-confluent.basic-auth.user-info' = 'u:p'", ["avro-confluent.basic-auth.user-info"]),
    ("... 'username' = 'lake_writer'", []),
    ("... 'password' = ''", []),
    ("-- 'password' = 'in a comment'", []),
    ("... 'scan.startup.mode' = 'latest-offset', 'topic' = 'orders'", []),
])
def test_credentials_written_in_sql_are_detected_by_option_name_only(sql, names):
    assert pg_lake.embedded_credentials(sql) == names


def test_a_detected_credential_never_echoes_its_value():
    msg = "; ".join(pg_lake.sql_problems("... 'password' = 'hunter2'", configured()))
    assert "hunter2" not in msg and "password" in msg


@pytest.mark.parametrize("sql,names", [
    ("'url' = '${PG_LAKE_JDBC_URL}'", ["PG_LAKE_JDBC_URL"]),
    ("SELECT '${A}', '${B}', '${A}'", ["A", "B"]),
    ("-- ${IN_A_COMMENT}\nSELECT 1", []),
    ("SELECT 1", []),
])
def test_unresolved_placeholders(sql, names):
    assert pg_lake.unresolved_placeholders(sql) == names


def test_the_old_template_is_refused_for_what_it_is():
    problems = pg_lake.sql_problems(OLD_TEMPLATE_SQL, configured())
    text = "; ".join(problems)
    assert "${PG_PASSWORD}" in text and "carries a credential" in text and "pg_lake catalog" in text


def test_a_governed_reference_to_an_unconfigured_runner_is_a_clear_error():
    (problem,) = pg_lake.sql_problems(GOOD_SQL, Settings())
    assert "not configured" in problem and "PG_LAKE_PASSWORD" in problem


def test_clean_sql_has_no_problems():
    assert pg_lake.sql_problems(GOOD_SQL, configured()) == []
    assert pg_lake.sql_problems("INSERT INTO sink SELECT 1;", Settings()) == []     # no pg_lake use: nothing required


@pytest.mark.parametrize("length", [80, 200, 10_000])
def test_scrub_hides_the_statement_and_the_password_but_keeps_the_reason(length):
    s = configured()
    stmt = pg_lake.catalog_statement(s)
    out = pg_lake.scrub(f"Flink SQL error in `{stmt[:length]}`: password authentication failed for {PASSWORD}", s)
    assert PASSWORD not in out and "lake_writer" not in out and "base-url" not in out
    assert "options hidden" in out and "authentication failed" in out


def test_the_template_no_longer_asks_for_credentials():
    (t,) = [t for t in SQL_TEMPLATES if t.name == "pg_lake PostgreSQL sink"]
    low = t.sql.lower()
    assert "${" not in t.sql and "password" not in low.replace("do not put a url, user or password", "") and "username" not in low
    assert "pg_lake.{DATABASE}.{PG_TABLE}" in t.sql
    assert {"PG_LAKE_JDBC_URL", "PG_USER", "PG_PASSWORD"}.isdisjoint(t.placeholders)


# ---- validation ------------------------------------------------------------
class _Gateway:
    """Records what a validation session is asked to run."""

    def __init__(self, fail_catalog=False):
        self.statements: list[str] = []
        self.fail_catalog = fail_catalog

    async def create_session(self, name=None):
        return "s1"

    async def close_session(self, sid):
        pass

    async def execute_statements(self, sid, sql, **kw):
        self.statements.append(sql)
        if self.fail_catalog and sql.startswith("CREATE CATALOG"):
            raise RuntimeError(f"Flink SQL error in `{sql[:80]}`: ['Internal server error.', '<Exception on server side:\\n"
                               f"org.apache.flink.X: Failed to fetchResults.\\n\\tat A(A.java:1)\\n"
                               f"Caused by: org.postgresql.util.PSQLException: FATAL: password authentication failed ({PASSWORD})\\n\\tat B']")
        return []

    async def explain_sql(self, sid, sql):
        self.statements.append("EXPLAIN " + sql)
        return {"valid": True, "errors": []}


@pytest.fixture
def gateway(monkeypatch):
    g = _Gateway()
    monkeypatch.setattr("app.services.validate_service.get_flink_client", lambda: g)
    return g


async def test_validation_refuses_the_old_template_without_touching_the_cluster(gateway, pg_settings):
    result = await validate_job_sql(OLD_TEMPLATE_SQL)
    assert result["valid"] is False and gateway.statements == []
    assert any("${PG_PASSWORD}" in e for e in result["errors"]) and any("credential" in e for e in result["errors"])
    assert "hunter" not in json.dumps(result)


async def test_validation_registers_the_catalog_before_the_users_statements(gateway, pg_settings):
    result = await validate_job_sql(GOOD_SQL)
    assert result["valid"] is True, result
    assert gateway.statements[0].startswith("CREATE CATALOG `pg_lake`")
    assert any(s.startswith("EXPLAIN INSERT INTO pg_lake.analytics.orders_sink") for s in gateway.statements[1:])
    assert PASSWORD not in json.dumps(result)                   # the password stays out of what the API returns
    assert result["detected_sinks"] == ["pg_lake.analytics.orders_sink"]


async def test_validation_without_pg_lake_use_never_creates_the_catalog(gateway, pg_settings):
    await validate_job_sql("CREATE TEMPORARY TABLE s (id INT) WITH ('connector'='datagen');\nINSERT INTO s SELECT 1;")
    assert not any(s.startswith("CREATE CATALOG") for s in gateway.statements)


async def test_validation_on_an_unconfigured_runner_says_so(gateway, unconfigured):
    result = await validate_job_sql(GOOD_SQL)
    assert result["valid"] is False and "not configured" in result["errors"][0] and gateway.statements == []


async def test_a_catalog_failure_never_leaks_the_password(monkeypatch, pg_settings):
    g = _Gateway(fail_catalog=True)
    monkeypatch.setattr("app.services.validate_service.get_flink_client", lambda: g)
    result = await validate_job_sql(GOOD_SQL)
    assert result["valid"] is False and "could not register" in result["errors"][0]
    assert "PSQLException: FATAL: password authentication failed" in result["errors"][0]      # the real cause is shown
    assert "Failed to fetchResults" not in result["errors"][0]                                  # not the stack-trace start
    assert PASSWORD not in json.dumps(result)


# ---- deploy ----------------------------------------------------------------
@pytest.fixture
def flink_db(pg_db):
    return pg_db


@pytest.fixture
def no_dispatch():
    """FL-011: deploy() only persists the work (nothing starts it until the leader loop runs), so there is nothing to stop; kept so the
    tests that name it still read as \"the deploy is not executed here\"."""
    yield


async def _job(db_maker, sql, name="orders") -> uuid.UUID:
    async with db_maker() as db:
        job = FlinkJob(org_id=get_settings().org_id, name=f"{name}-{uuid.uuid4().hex[:6]}", sql=sql, status="validated")
        db.add(job)
        await db.commit()
        return job.id


async def _deploy(db_maker, job_id):
    async with db_maker() as db:
        dep = await JobService(db).deploy(get_settings().org_id, job_id, "u1", DeployRequest())
        await db.commit()
        return dep.id


async def test_deploy_refuses_sql_with_a_credential_or_an_unresolved_placeholder(flink_db, no_dispatch, pg_settings):
    from fastapi import HTTPException
    job_id = await _job(flink_db, OLD_TEMPLATE_SQL)
    with pytest.raises(HTTPException) as e:
        await _deploy(flink_db, job_id)
    assert e.value.status_code == 422 and "${PG_PASSWORD}" in e.value.detail["message"]
    async with flink_db() as db:
        assert (await db.execute(select(FlinkDeployment))).scalars().all() == []
        assert (await db.get(FlinkJob, job_id)).status == "validated"        # untouched


async def test_deploy_on_an_unconfigured_runner_is_refused(flink_db, no_dispatch, unconfigured):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        await _deploy(flink_db, await _job(flink_db, GOOD_SQL))
    assert e.value.status_code == 422 and "not configured" in e.value.detail["message"]


async def test_the_catalog_is_registered_in_the_session_and_the_password_is_stored_nowhere(flink_db, no_dispatch,
                                                                                         pg_settings, monkeypatch):
    fake = FakeFlinkClient()
    patch_flink_client(monkeypatch, fake)
    job_id = await _job(flink_db, GOOD_SQL)
    dep_id = await _deploy(flink_db, job_id)
    await _execute_deploy(dep_id)

    assert fake.submitted_sql[0].startswith("CREATE CATALOG `pg_lake`") and PASSWORD in fake.submitted_sql[0]
    assert "INSERT INTO pg_lake.analytics.orders_sink" in fake.submitted_sql[1] and PASSWORD not in fake.submitted_sql[1]
    async with flink_db() as db:
        job, dep = await db.get(FlinkJob, job_id), await db.get(FlinkDeployment, dep_id)
        stored = json.dumps([job.sql, job.description, dep.manifest, dep.error_message, dep.submission_tag], default=str)
        assert dep.status == "running"
    assert PASSWORD not in stored and "lake_writer" not in stored


async def test_a_job_that_does_not_use_pg_lake_gets_no_catalog(flink_db, no_dispatch, pg_settings, monkeypatch):
    fake = FakeFlinkClient()
    patch_flink_client(monkeypatch, fake)
    dep_id = await _deploy(flink_db, await _job(flink_db, "INSERT INTO sink SELECT * FROM src;"))
    await _execute_deploy(dep_id)
    assert not any(s.startswith("CREATE CATALOG") for s in fake.submitted_sql)


async def test_a_failing_catalog_fails_the_deployment_without_leaking_or_submitting(flink_db, no_dispatch, pg_settings,
                                                                                    monkeypatch):
    class Failing(FakeFlinkClient):
        async def execute_statements(self, session_id, sql, on_submit=None):
            if sql.startswith("CREATE CATALOG"):
                raise RuntimeError(f"Flink SQL error in `{sql[:300]}`: connection refused to pg:5432 as {PASSWORD}")
            return await super().execute_statements(session_id, sql, on_submit=on_submit)

    fake = Failing()
    patch_flink_client(monkeypatch, fake)
    job_id = await _job(flink_db, GOOD_SQL)
    dep_id = await _deploy(flink_db, job_id)
    await _execute_deploy(dep_id)
    async with flink_db() as db:
        dep = await db.get(FlinkDeployment, dep_id)
        assert dep.status == "failed" and "could not register" in dep.error_message
        assert PASSWORD not in dep.error_message and (await db.get(FlinkJob, job_id)).status == "failed"
    assert fake.submitted_sql == []                          # the user's SQL never ran


def test_scrub_removes_the_password_even_when_no_statement_is_present():
    """The error text can echo only the password (a driver message), with no CREATE CATALOG in it."""
    s = configured()
    out = pg_lake.scrub(f"password authentication failed for user lake_writer ({PASSWORD}); retry '{PASSWORD}'", s)
    assert PASSWORD not in out and out.count("***") == 2


def test_the_error_shows_the_real_cause_not_the_start_of_a_java_stack_trace():
    trace = ("Flink SQL error in `CREATE CATALOG`: ['Internal server error.', '<Exception on server side:\\n"
             "org.apache.flink.table.gateway.api.utils.SqlGatewayException: Failed to fetchResults.\\n\\tat org.apache.X(X.java:1)\\n"
             "Caused by: org.apache.flink.table.api.ValidationException: could not open the catalog\\n\\tat Y\\n"
             "Caused by: org.postgresql.util.PSQLException: FATAL: password authentication failed for user \"lake_writer\"\\n\\tat Z']")
    assert pg_lake._reason(trace) == 'org.postgresql.util.PSQLException: FATAL: password authentication failed for user "lake_writer"'
    assert pg_lake._reason("plain error") == "plain error"
    assert len(pg_lake._reason("x" * 5000)) == 400
