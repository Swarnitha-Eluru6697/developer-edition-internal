"""Engine "clickhouse": how dbt, SQL jobs, Python jobs, registration and Kubernetes Jobs reach a ClickHouse server.

A real ClickHouse is covered by the live test in the bug log; here: the profile dbt gets (password from the env, never written into the
file), the SQL job's one-statement-at-a-time behaviour and its connection settings, the variables a Python job gets (and the password it
may print being hidden), the clear error when ClickHouse is not configured, what a Kubernetes Job gets, and that a job can be registered.
"""
from __future__ import annotations

import sys
import types
import uuid

import pytest
import yaml

from app.core.config import get_settings
from app.executors import dbt_executor, isolation, sql_job_executor
from app.executors.python_job_executor import _secret_values, engine_environment
from tests.test_isolation import _env, _k8s_settings, no_real_k8s  # noqa: F401  (autouse: never a real kube config)
from tests.test_sql_jobs import api  # noqa: F401  (fixture: the runner app behind an ASGI client)


@pytest.fixture
def ch(monkeypatch):
    s = get_settings()
    for k, v in dict(clickhouse_host="clickhouse.internal", clickhouse_port=8123, clickhouse_secure=False, clickhouse_user="inaiera",
                     clickhouse_password="ch-secret", clickhouse_database="analytics").items():
        monkeypatch.setattr(s, k, v)
    return s


def _output():
    return yaml.safe_load(dbt_executor._profiles_yml("clickhouse"))["inaiera"]["outputs"]["prod"]


def test_the_profile_points_dbt_at_the_configured_clickhouse_and_keeps_the_password_out_of_the_file(ch):
    out = _output()
    assert (out["type"], out["host"], out["port"], out["user"], out["schema"], out["secure"]) == (
        "clickhouse", "clickhouse.internal", 8123, "inaiera", "analytics", False)
    assert out["password"] == "{{ env_var('CLICKHOUSE_PASSWORD', '') }}"
    assert "ch-secret" not in dbt_executor._profiles_yml("clickhouse")


def test_models_land_in_the_configured_database_not_in_a_database_called_public(ch):
    assert _output()["schema"] == "analytics"
    assert yaml.safe_load(dbt_executor._profiles_yml("clickhouse", schema="staging"))["inaiera"]["outputs"]["prod"]["schema"] == "staging"


def test_tls_is_switched_on_in_the_profile(ch, monkeypatch):
    monkeypatch.setattr(ch, "clickhouse_secure", True)
    monkeypatch.setattr(ch, "clickhouse_port", 8443)
    out = _output()
    assert out["secure"] is True and out["port"] == 8443


def test_a_runner_without_clickhouse_says_so_instead_of_writing_host_none(ch, monkeypatch):
    monkeypatch.setattr(ch, "clickhouse_host", None)
    with pytest.raises(ValueError, match="CLICKHOUSE_HOST"):
        dbt_executor._profiles_yml("clickhouse")


class _FakeClient:
    """clickhouse_connect's client: records the statements, fails on the one containing 'boom'."""
    def __init__(self, seen):
        self.seen = seen
        self.closed = False

    def command(self, statement):
        self.seen["statements"].append(statement)
        if "boom" in statement:
            raise RuntimeError("Code: 62. DB::Exception: Syntax error near boom")
        # What clickhouse_connect really returns for DDL and INSERT alike: a summary, with written_rows 0 for DDL.
        return types.SimpleNamespace(written_rows=3 if statement.lower().startswith("insert") else 0)

    def close(self):
        self.closed = True


def _fake_clickhouse(monkeypatch):
    seen = {"statements": [], "kwargs": {}}
    client = _FakeClient(seen)

    def get_client(**kw):
        seen["kwargs"] = kw
        return client
    monkeypatch.setitem(sys.modules, "clickhouse_connect", types.SimpleNamespace(get_client=get_client))
    seen["client"] = client
    return seen


def test_a_sql_job_connects_with_the_runners_settings(ch, monkeypatch):
    seen = _fake_clickhouse(monkeypatch)
    sql_job_executor._clickhouse_connect(ch)
    kw = seen["kwargs"]
    assert (kw["host"], kw["port"], kw["secure"], kw["username"], kw["password"], kw["database"]) == (
        "clickhouse.internal", 8123, False, "inaiera", "ch-secret", "analytics")


def test_a_sql_job_without_clickhouse_configured_fails_with_the_reason(ch, monkeypatch, tmp_path):
    monkeypatch.setattr(ch, "clickhouse_host", None)
    f = tmp_path / "a.sql"
    f.write_text("select 1;")
    r = sql_job_executor.run_sql_file(engine="clickhouse", sql_path=f)
    assert r["success"] is False and "CLICKHOUSE_HOST" in r["errors"][0]


def test_every_statement_runs_in_order_and_the_row_count_of_an_insert_is_reported(ch, monkeypatch, tmp_path):
    seen = _fake_clickhouse(monkeypatch)
    f = tmp_path / "a.sql"
    f.write_text("create table marts.t (x Int32) engine = MergeTree order by x;\ninsert into marts.t select x from raw.s;")
    r = sql_job_executor.run_sql_file(engine="clickhouse", sql_path=f)
    assert r["success"] is True and [n["status"] for n in r["nodes"]] == ["success", "success"]
    assert len(seen["statements"]) == 2 and seen["client"].closed
    assert "3 rows" in r["nodes"][1]["message"] and "row" not in r["nodes"][0]["message"]          # DDL says what it was, not "0 rows"
    assert r["lineage"]                       # the job's data lineage is still derived from its statements


def test_the_first_failing_statement_stops_the_job_and_what_ran_before_stays_done(ch, monkeypatch, tmp_path):
    seen = _fake_clickhouse(monkeypatch)
    f = tmp_path / "a.sql"
    f.write_text("select 1;\nselect boom;\nselect 3;")
    r = sql_job_executor.run_sql_file(engine="clickhouse", sql_path=f)
    assert r["success"] is False
    assert [n["status"] for n in r["nodes"]] == ["success", "error", "skipped"]
    assert "statement 2 failed (statements 1-1 had already run)" in r["errors"][0] and "Syntax error" in r["errors"][0]
    assert len(seen["statements"]) == 2 and seen["client"].closed          # the third statement was never sent


def test_a_python_job_is_given_the_connection_and_a_printed_password_is_hidden(ch):
    env = engine_environment("clickhouse")
    assert env["INAIERA_ENGINE"] == "clickhouse" and env["INAIERA_CLICKHOUSE_HOST"] == "clickhouse.internal"
    assert (env["INAIERA_CLICKHOUSE_PORT"], env["INAIERA_CLICKHOUSE_SECURE"], env["INAIERA_CLICKHOUSE_USER"],
            env["INAIERA_CLICKHOUSE_DATABASE"], env["INAIERA_CLICKHOUSE_PASSWORD"]) == ("8123", "false", "inaiera", "analytics", "ch-secret")
    assert _secret_values(env) == ["ch-secret"] and "PGHOST" not in env and "INAIERA_TRINO_HOST" not in env


def _job_env(**kw):
    job = isolation._build_job(run_id=uuid.uuid4(), attempt=1, engine="clickhouse", select=None, settings=_k8s_settings(**kw))
    return _env(job)


def test_a_kubernetes_job_gets_the_connection_and_the_password_from_a_secret():
    env = _job_env(clickhouse_host="ch.svc", clickhouse_port=8443, clickhouse_secure=True, clickhouse_password_secret_name="ch-creds")
    assert env["CLICKHOUSE_HOST"].value == "ch.svc" and env["CLICKHOUSE_PORT"].value == "8443" and env["CLICKHOUSE_SECURE"].value == "true"
    assert env["CLICKHOUSE_PASSWORD"].value is None and env["CLICKHOUSE_PASSWORD"].value_from.secret_key_ref.name == "ch-creds"


def test_a_kubernetes_job_in_dev_may_carry_a_plain_password_but_never_outside_dev():
    assert _job_env(clickhouse_host="ch.svc", clickhouse_password="dev-pw")["CLICKHOUSE_PASSWORD"].value == "dev-pw"
    settings = _k8s_settings(environment="staging", clickhouse_host="ch.svc", clickhouse_password="leak",
                             artifact_store_backend="s3", artifact_s3_bucket="b")
    with pytest.raises(isolation.JobSpecError, match="CLICKHOUSE_PASSWORD_SECRET_NAME"):
        isolation._build_job(run_id=uuid.uuid4(), attempt=1, engine="clickhouse", select=None, settings=settings)


@pytest.mark.parametrize("kind,path", [("dbt", "customers"), ("sql", "jobs/daily.sql"), ("python", "jobs/etl.py")])
async def test_a_job_can_be_registered_on_clickhouse(api, auth_headers, kind, path):
    body = {"git_url": f"https://github.com/acme/jobs-{uuid.uuid4().hex[:6]}.git", "git_ref": "main", "engine": "clickhouse",
            "kind": kind, "project_path": path}
    r = await api.post("/api/v1/projects/register", headers=auth_headers, json=body)
    assert r.status_code == 201, r.text
    assert r.json()["engine"] == "clickhouse" and r.json()["kind"] == kind


async def test_an_unknown_engine_is_still_refused(api, auth_headers):
    body = {"git_url": "https://github.com/acme/x.git", "git_ref": "main", "engine": "oracle", "project_path": "a"}
    assert (await api.post("/api/v1/projects/register", headers=auth_headers, json=body)).status_code == 422
