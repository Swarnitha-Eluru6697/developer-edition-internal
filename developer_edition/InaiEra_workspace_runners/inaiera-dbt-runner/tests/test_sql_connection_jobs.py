"""SQL jobs on a Workspace connection, from the runner's side: it holds no credentials, it asks the Workspace to run the statements.

Registered with `connection_id`, frozen into the run's manifest, and executed by sending the file's statements to the Workspace with
a token minted for exactly this run. The Workspace is faked at the HTTP layer; the other half is tested in the Workspace repo.
"""
from __future__ import annotations

import uuid
from pathlib import Path

import httpx
import pytest
from jose import jwt

from app.core.config import Settings, get_settings
from app.executors import connection_sql_job as csj
from app.executors import isolation
from app.main import app
from app.models import Project, Run
from app.services import run_manifest
from app.services.run_service import dispatch_run, freeze_project
from tests.test_isolation import no_real_k8s  # noqa: F401  (autouse: never a real kube config)

SECRET = "workspace-job-secret-0123456789abcdef"
CONN = str(uuid.uuid4())


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "workspace_url", "http://workspace.test")
    monkeypatch.setattr(s, "workspace_job_jwt_secret", SECRET)


@pytest.fixture
async def api(run_db):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


class Workspace:
    """The Workspace as the runner sees it: records each POST and answers as configured."""
    def __init__(self):
        self.calls: list[dict] = []
        self.reply = httpx.Response(200, json={"success": True, "errors": [], "nodes": [{"node_id": "statement.1", "status": "success", "message": "INSERT · 2 rows"}]})

    def post(self, url, *, json, headers, timeout):
        self.calls.append({"url": url, "json": json, "token": headers["X-Internal-Service-Token"], "timeout": timeout})
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


@pytest.fixture
def workspace(monkeypatch):
    w = Workspace()
    monkeypatch.setattr(csj.httpx, "post", w.post)
    return w


def _sql(tmp_path: Path, text="create table t (a int);\ninsert into t values (1), (2);\n") -> Path:
    f = tmp_path / "jobs" / "a.sql"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text)
    return f


def _claims(token: str) -> dict:
    return jwt.decode(token, SECRET, algorithms=["HS256"], audience="workspace", issuer="dbtrunner")


# ── the run token ────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_the_token_names_this_run_its_job_and_its_connection_and_nothing_else():
    project, run = uuid.uuid4(), uuid.uuid4()
    c = _claims(csj.mint_run_token(project_id=project, run_id=run, connection_id=CONN))
    assert (c["iss"], c["sub"], c["aud"], c["scope"], c["org_id"]) == ("dbtrunner", "dbtrunner", "workspace", "sql.jobs.run", get_settings().org_id)
    assert (c["project_id"], c["run_id"], c["connection_id"]) == (str(project), str(run), CONN)
    assert c["exp"] - c["iat"] == get_settings().sql_job_statement_timeout_seconds + csj.SLACK_SECONDS and len(c["jti"]) == 32


def test_every_token_is_unique():
    mint = lambda: csj.mint_run_token(project_id=uuid.uuid4(), run_id=uuid.uuid4(), connection_id=CONN)  # noqa: E731
    assert _claims(mint())["jti"] != _claims(mint())["jti"]


def test_without_a_url_and_secret_there_is_no_token(monkeypatch):
    monkeypatch.setattr(get_settings(), "workspace_job_jwt_secret", None)
    with pytest.raises(RuntimeError, match="not configured on this runner"):
        csj.mint_run_token(project_id=uuid.uuid4(), run_id=uuid.uuid4(), connection_id=CONN)


def test_the_secret_may_not_be_shared_with_another_signing_secret():
    with pytest.raises(ValueError, match="WORKSPACE_JOB_JWT_SECRET must differ"):
        Settings(workspace_job_jwt_secret="same-secret-value", scheduler_jwt_secret="same-secret-value")


# ── asking the Workspace to run the file ─────────────────────────────────────────────────────────────────────────────────
def test_the_files_statements_go_to_the_workspace_with_the_token_and_its_answer_is_the_result(tmp_path, workspace):
    result = csj.run_sql_file_on_connection(sql_path=_sql(tmp_path), token="tok-1")
    (call,) = workspace.calls
    assert call["url"] == "http://workspace.test/api/v1/internal/jobs/sql" and call["token"] == "tok-1"
    assert call["json"] == {"statements": ["create table t (a int)", "insert into t values (1), (2)"],
                            "timeout_seconds": get_settings().sql_job_statement_timeout_seconds}
    assert result == {"success": True, "errors": [], "nodes": [{"node_id": "statement.1", "status": "success", "message": "INSERT · 2 rows"}],
                      "lineage": []}                                  # nothing here reads a schema-qualified table: no data lineage


def test_a_failure_the_workspace_reports_is_the_runs_failure(tmp_path, workspace):
    workspace.reply = httpx.Response(200, json={"success": False, "errors": ["statement 2 failed, nothing was committed: boom"],
                                                "nodes": [{"node_id": "statement.2", "status": "error", "message": "boom"}]})
    result = csj.run_sql_file_on_connection(sql_path=_sql(tmp_path), token="t")
    assert result["success"] is False and result["errors"] == ["statement 2 failed, nothing was committed: boom"]


@pytest.mark.parametrize("status,body,expected", [
    (403, {"detail": {"code": "CONNECTION_USE_DENIED", "message": "lithish may no longer use connection 'warehouse'"}}, "may no longer use connection"),
    (401, {"detail": {"code": "RUNNER_TOKEN_INVALID", "message": "the run token is invalid or has expired"}}, "HTTP 401"),
    (503, {"detail": "Service Unavailable"}, "HTTP 503"),
])
def test_a_refusal_is_reported_with_the_workspaces_reason(tmp_path, workspace, status, body, expected):
    workspace.reply = httpx.Response(status, json=body)
    result = csj.run_sql_file_on_connection(sql_path=_sql(tmp_path), token="t")
    assert result["success"] is False and result["nodes"] == [] and expected in result["errors"][0]


def test_an_unreachable_workspace_fails_the_run_with_a_reason(tmp_path, workspace):
    workspace.reply = httpx.ConnectError("refused")
    result = csj.run_sql_file_on_connection(sql_path=_sql(tmp_path), token="t")
    assert result["success"] is False and "could not be reached" in result["errors"][0]


def test_an_empty_file_or_a_missing_url_never_calls_the_workspace(tmp_path, workspace, monkeypatch):
    assert csj.run_sql_file_on_connection(sql_path=_sql(tmp_path, "-- nothing\n"), token="t")["errors"] == ["the SQL file has no statements"]
    monkeypatch.setattr(get_settings(), "workspace_url", None)
    assert "not configured" in csj.run_sql_file_on_connection(sql_path=_sql(tmp_path), token="t")["errors"][0]
    assert workspace.calls == []


# ── registration and the frozen manifest ─────────────────────────────────────────────────────────────────────────────────
def _body(**over):
    return {"git_url": f"https://github.com/acme/jobs-{uuid.uuid4().hex[:6]}.git", "git_ref": "main", "engine": "pg_lake", **over}


async def test_a_sql_job_is_registered_on_a_connection_and_lists_it(api, auth_headers):
    r = await api.post("/api/v1/projects/register", headers=auth_headers, json=_body(kind="sql", project_path="jobs/a.sql", connection_id=CONN))
    assert r.status_code == 201, r.text
    assert r.json()["connection_id"] == CONN
    assert (await api.get(f"/api/v1/projects/{r.json()['id']}", headers=auth_headers)).json()["connection_id"] == CONN
    plain = await api.post("/api/v1/projects/register", headers=auth_headers, json=_body(kind="sql", project_path="jobs/b.sql"))
    assert plain.json()["connection_id"] is None


@pytest.mark.parametrize("body", [{"kind": "python", "project_path": "jobs/a.py"}, {"kind": "dbt", "project_path": "customers"}])
async def test_only_sql_jobs_can_name_a_connection(api, auth_headers, body):
    r = await api.post("/api/v1/projects/register", headers=auth_headers, json=_body(connection_id=CONN, **body))
    assert r.status_code == 422 and "only for SQL jobs" in r.text


async def test_the_connection_is_frozen_into_the_runs_manifest(run_db):
    async with run_db() as db:
        on_conn = Project(org_id=get_settings().org_id, name="a", kind="sql", project_path="a.sql", engine="pg_lake", connection_id=uuid.UUID(CONN),
                          git_url="https://github.com/acme/a.git", git_ref="main")
        plain = Project(org_id=get_settings().org_id, name="b", kind="sql", project_path="b.sql", engine="pg_lake",
                        git_url="https://github.com/acme/b.git", git_ref="main")
        db.add_all([on_conn, plain])
        await db.commit()
        m1, m2 = await freeze_project(db, on_conn.id, None), await freeze_project(db, plain.id, None)
    assert run_manifest.job_connection(m1) == CONN and run_manifest.job_connection(m2) is None and "connection_id" not in m2


# ── dispatch ─────────────────────────────────────────────────────────────────────────────────────────────────────────────
async def test_a_job_on_a_connection_runs_through_the_workspace_and_never_on_the_runners_own_engine(run_db, tmp_path, monkeypatch, workspace):
    from tests.test_sql_jobs import _fake_git

    _sql(tmp_path)
    _fake_git(monkeypatch, tmp_path)
    monkeypatch.setattr("app.executors.sql_job_executor.run_sql_file", lambda **kw: pytest.fail("the runner's own engine must not run it"))
    async with run_db() as db:
        p = Project(org_id=get_settings().org_id, name="a", kind="sql", project_path="jobs/a.sql", engine="pg_lake", connection_id=uuid.UUID(CONN),
                    git_url="https://github.com/acme/a.git", git_ref="main")
        db.add(p)
        await db.flush()
        run = Run(project_id=p.id, trigger="scheduled", status="queued", attempt=0)
        db.add(run)
        await db.commit()
    await dispatch_run(run.id)
    async with run_db() as db:
        done = await db.get(Run, run.id)
    assert done.status == "succeeded" and done.models_total == 1
    (call,) = workspace.calls
    claims = _claims(call["token"])
    assert (claims["project_id"], claims["run_id"], claims["connection_id"]) == (str(p.id), str(run.id), CONN)
    assert call["json"]["statements"] == ["create table t (a int)", "insert into t values (1), (2)"]


async def test_a_workspace_refusal_fails_the_run_and_says_why(run_db, tmp_path, monkeypatch, workspace):
    from tests.test_sql_jobs import _fake_git

    _sql(tmp_path)
    _fake_git(monkeypatch, tmp_path)
    workspace.reply = httpx.Response(403, json={"detail": {"code": "JOB_NOT_BOUND", "message": "This job is not bound to that connection"}})
    async with run_db() as db:
        p = Project(org_id=get_settings().org_id, name="a", kind="sql", project_path="jobs/a.sql", engine="pg_lake", connection_id=uuid.UUID(CONN),
                    git_url="https://github.com/acme/a.git", git_ref="main")
        db.add(p)
        await db.flush()
        run = Run(project_id=p.id, trigger="scheduled", status="queued", attempt=0)
        db.add(run)
        await db.commit()
    await dispatch_run(run.id)
    async with run_db() as db:
        done = await db.get(Run, run.id)
    assert done.status == "failed" and "not bound to that connection" in done.error_message


# ── Kubernetes ───────────────────────────────────────────────────────────────────────────────────────────────────────────
async def _k8s_run(monkeypatch, *, kind, connection_id):
    from tests.conftest import FAKE_COMMIT_SHA
    from tests.test_isolation import FakeK8s, FakeLease, _inline_log, _k8s_settings, _use

    fake = FakeK8s(statuses=[(1, None)], pod_log=_inline_log({"success": True, "errors": [], "nodes": []}))
    _use(monkeypatch, fake, _k8s_settings(workspace_url="http://workspace.test"))
    built = []
    real = isolation._build_job

    def spy(**kw):
        job = real(**kw)
        built.append({e.name: e for e in job.spec.template.spec.containers[0].env})
        return job
    monkeypatch.setattr(isolation, "_build_job", spy)
    run_id, project_id = uuid.uuid4(), uuid.uuid4()
    await isolation.RunnerIsolation().execute_run(
        run_id=run_id, project_dir=None, engine="pg_lake", select=None, git_url="https://github.com/acme/jobs.git", git_ref="main",
        project_id=project_id, lease=FakeLease(), project_path="jobs/a.sql", git_sha=FAKE_COMMIT_SHA, dq_schema_blocks={}, kind=kind,
        connection_id=connection_id)
    return fake, built[0], run_id, project_id


async def test_the_kubernetes_job_gets_the_run_token_in_its_secret_and_where_the_workspace_is(monkeypatch):
    fake, env, run_id, project_id = await _k8s_run(monkeypatch, kind="sql", connection_id=CONN)
    token = fake.secrets[isolation._secret_name(run_id, 1)].string_data["WORKSPACE_JOB_TOKEN"]
    c = _claims(token)
    assert (c["project_id"], c["run_id"], c["connection_id"]) == (str(project_id), str(run_id), CONN)
    assert env["RUNNER_WORKSPACE_JOB_TOKEN"].value_from.secret_key_ref.key == "WORKSPACE_JOB_TOKEN"        # a secret reference, never a plain value
    assert env["WORKSPACE_URL"].value == "http://workspace.test"


@pytest.mark.parametrize("kind,connection_id", [("sql", None), ("python", None), ("dbt", None)])
async def test_a_job_that_is_not_on_a_connection_gets_no_token(monkeypatch, kind, connection_id):
    fake, env, run_id, _ = await _k8s_run(monkeypatch, kind=kind, connection_id=connection_id)
    assert fake.secrets[isolation._secret_name(run_id, 1)].string_data["WORKSPACE_JOB_TOKEN"] == ""
    assert "WORKSPACE_URL" not in env


def test_the_job_entrypoint_hands_a_connection_job_to_the_workspace_instead_of_the_local_engine(tmp_path, monkeypatch, capsys):
    from app.executors import job_entrypoint
    from tests.conftest import FAKE_COMMIT_SHA
    from tests.test_sql_jobs import _fake_git

    _sql(tmp_path)
    _fake_git(monkeypatch, tmp_path)
    seen = {}

    def fake_remote(*, sql_path, token):
        seen.update(path=sql_path, token=token)
        return {"success": True, "errors": [], "nodes": [{"node_id": "statement.1", "status": "success"}]}
    monkeypatch.setattr("app.executors.connection_sql_job.run_sql_file_on_connection", fake_remote)
    monkeypatch.setattr("app.executors.sql_job_executor.run_sql_file", lambda **kw: pytest.fail("the pod's own engine must not run it"))
    monkeypatch.setattr(job_entrypoint, "get_dbt_executor", lambda: pytest.fail("dbt must not run"))
    for k, v in {"RUNNER_GIT_URL": "https://github.com/acme/jobs.git", "RUNNER_GIT_REF": "main", "RUNNER_GIT_SHA": FAKE_COMMIT_SHA,
                 "RUNNER_PROJECT_PATH": "jobs/a.sql", "RUNNER_WORKSPACE_JOB_TOKEN": "run-token"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(get_settings(), "artifact_store_backend", "none")
    assert job_entrypoint.main(["--run-id", str(uuid.uuid4()), "--engine", "pg_lake", "--kind", "sql"]) == 0
    assert seen["token"] == "run-token" and seen["path"].name == "a.sql"
