"""SQL jobs that run on a Workspace connection (Database Explorer).

The dbt runner holds no connection credentials. A job is registered through the Workspace (which checks the caller may use the
connection and records who they are, JobConnectionBinding); when the job runs, the runner sends its statements here with a one-run
token, and the Workspace runs them on the connection as the registered user — in one transaction — after checking that user may
still use it. Covered: the token, the registration checks and binding, the internal endpoint's authorization, and the executor
against a real Postgres (the throwaway `dgx-testdb` when TEST_PG_URL is set).
"""
from __future__ import annotations

import json
import os
import time
import uuid
from types import SimpleNamespace

import pytest
from fastapi import Response
from fastapi.testclient import TestClient
from jose import jwt

from app.api import dbt_proxy
from app.core import runner_job_token as rjt
from app.core.config import get_settings
from app.core.deps import get_db
from app.executors import connection_job_executor as cje
from app.main import app
from app.models import DatabaseConnection, JobConnectionBinding, User
from app.services import audit_service, ucs_client, workspace_service
from conftest import TEST_ORG_ID, _headers

SECRET = "runner-job-secret-0123456789abcdef"
ADMIN = {"roles": ["workspace_admin"]}
PROJECT, RUN, CONN = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
USER_ID = uuid.uuid4()


def _token(**over) -> str:
    now = int(time.time())
    claims = {"iss": "dbtrunner", "sub": "dbtrunner", "aud": "workspace", "scope": "sql.jobs.run", "org_id": TEST_ORG_ID,
              "project_id": str(PROJECT), "run_id": str(RUN), "connection_id": str(CONN), "iat": now, "exp": now + 3600,
              "jti": uuid.uuid4().hex}
    claims.update(over)
    return jwt.encode({k: v for k, v in claims.items() if v is not None}, SECRET, algorithm="HS256")


@pytest.fixture(autouse=True)
def secret(monkeypatch):
    monkeypatch.setattr(get_settings(), "runner_job_jwt_secret", SECRET)


class FakeRedis:
    def __init__(self, down=False):
        self.keys, self.down = set(), down

    async def set(self, key, value, nx=False, ex=None):
        if self.down:
            raise ConnectionError("redis down")
        if nx and key in self.keys:
            return None
        self.keys.add(key)
        return True


@pytest.fixture
def redis(monkeypatch):
    import app.services.execution_router as router_mod
    r = FakeRedis()
    monkeypatch.setattr(router_mod, "get_redis", lambda: r)
    return r


# ── the run token ────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_a_valid_token_is_accepted_and_names_its_job():
    claims = rjt.decode_runner_job_token(_token())
    assert (claims["project_id"], claims["run_id"], claims["connection_id"]) == (str(PROJECT), str(RUN), str(CONN))


@pytest.mark.parametrize("over,status,code", [
    ({"aud": "someone-else"}, 401, "RUNNER_TOKEN_INVALID"), ({"iss": "workspace"}, 401, "RUNNER_TOKEN_INVALID"),
    ({"exp": int(time.time()) - 600, "iat": int(time.time()) - 900}, 401, "RUNNER_TOKEN_INVALID"),
    ({"scope": "dbt.projects.write"}, 403, "RUNNER_TOKEN_SCOPE"), ({"org_id": "another-org"}, 403, "ORG_MISMATCH"),
    ({"exp": int(time.time()) + 5 * 3600}, 401, "RUNNER_TOKEN_INVALID"), ({"connection_id": "not-a-uuid"}, 401, "RUNNER_TOKEN_INVALID"),
    ({"project_id": None}, 401, "RUNNER_TOKEN_INVALID"), ({"jti": None}, 401, "RUNNER_TOKEN_INVALID"),
])
def test_a_token_that_is_not_the_runners_one_run_credential_is_refused(over, status, code):
    with pytest.raises(rjt.WorkspaceError) as err:
        rjt.decode_runner_job_token(_token(**over))
    assert err.value.status_code == status and err.value.detail["code"] == code


def test_a_token_signed_with_another_key_or_none_at_all_is_refused():
    other = jwt.encode({"iss": "dbtrunner", "aud": "workspace"}, "some-other-key-some-other-key-xx", algorithm="HS256")
    for bad in (other, None, "garbage"):
        with pytest.raises(rjt.WorkspaceError) as err:
            rjt.decode_runner_job_token(bad)
        assert err.value.status_code == 401


def test_without_a_configured_secret_the_feature_is_off(monkeypatch):
    monkeypatch.setattr(get_settings(), "runner_job_jwt_secret", None)
    with pytest.raises(rjt.WorkspaceError) as err:
        rjt.decode_runner_job_token(_token())
    assert err.value.status_code == 503 and err.value.detail["code"] == "RUNNER_JOBS_NOT_CONFIGURED"


async def test_a_token_works_once(redis):
    claims = rjt.decode_runner_job_token(_token())
    await rjt.consume_jti(claims)
    with pytest.raises(rjt.WorkspaceError) as err:
        await rjt.consume_jti(claims)
    assert err.value.detail["code"] == "RUNNER_TOKEN_REPLAYED"


async def test_when_the_replay_store_is_down_nothing_runs(redis):
    redis.down = True
    with pytest.raises(rjt.WorkspaceError) as err:
        await rjt.consume_jti(rjt.decode_runner_job_token(_token()))
    assert err.value.status_code == 503


def test_the_secret_may_not_be_shared_with_another_service_secret():
    from app.core.config import Settings
    with pytest.raises(ValueError, match="RUNNER_JOB_JWT_SECRET must differ"):
        Settings(runner_job_jwt_secret="same-secret", dq_delegation_jwt_secret="same-secret")


# ── the internal endpoint ────────────────────────────────────────────────────────────────────────────────────────────────
class Db:
    """A session that answers db.get() for the rows a test sets up and records what is added or executed."""
    def __init__(self, rows):
        self.rows, self.added, self.executed, self.rolled_back, self.fail_execute = rows, [], [], 0, False

    async def get(self, model, key):
        return self.rows.get((model, key))

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass

    async def execute(self, stmt):
        self.executed.append(stmt)
        if self.fail_execute:
            raise RuntimeError("no such table")

    async def rollback(self):
        self.rolled_back += 1


def _conn(**over):
    base = dict(id=CONN, org_id=TEST_ORG_ID, name="warehouse", connection_type="postgres", host="pg.internal", port=5432,
                database_name="db", use_tls=False, vault_secret_path="p", created_by_user_id=USER_ID)
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture
def world(monkeypatch, redis):
    user = SimpleNamespace(id=USER_ID, external_id="u-1", username="lithish", email="l@x")
    binding = SimpleNamespace(project_id=PROJECT, connection_id=CONN, user_id=USER_ID, groups='["/finance", "/analytics"]',
                              permissions='["workspace:access:view", "workspace:sql:run"]')
    db = Db({(JobConnectionBinding, PROJECT): binding, (DatabaseConnection, CONN): _conn(), (User, USER_ID): user})
    ran, audits, authorized = [], [], []

    async def fake_run(*, conn, statements, timeout_seconds):
        ran.append((conn.name, list(statements), timeout_seconds))
        return {"success": True, "errors": [], "nodes": [{"node_id": "statement.1", "status": "success", "message": "INSERT · 2 rows"}]}

    async def fake_authorize(principal, user_row_id, conn):
        authorized.append((principal, user_row_id, conn.name))
        if getattr(fake_authorize, "deny", None):
            raise ucs_client.UcsAuthorizationError(fake_authorize.deny)

    async def record(self, **kw):
        audits.append(kw)
    monkeypatch.setattr("app.api.internal_jobs.run_job_statements", fake_run)
    monkeypatch.setattr("app.api.internal_jobs.authorize_connection_use", fake_authorize)
    monkeypatch.setattr(audit_service.AuditService, "record", record)

    async def _db():
        yield db
    app.dependency_overrides[get_db] = _db
    yield SimpleNamespace(db=db, ran=ran, audits=audits, authorized=authorized, binding=binding, authz=fake_authorize, client=TestClient(app))
    app.dependency_overrides.clear()


def _post(w, token=None, statements=("insert into t values (1), (2)",), **extra):
    return w.client.post("/api/v1/internal/jobs/sql", json={"statements": list(statements), **extra},
                         headers={"X-Internal-Service-Token": token or _token()})


def test_a_bound_job_runs_its_statements_on_the_connection_as_the_registered_user(world):
    r = _post(world, timeout_seconds=900)
    assert r.status_code == 200 and r.json()["success"] is True
    assert world.ran == [("warehouse", ["insert into t values (1), (2)"], 900)]
    principal, user_row_id, conn_name = world.authorized[0]
    assert (principal.user_id, principal.email, principal.groups, principal.org_id) == ("u-1", "lithish", ["/finance", "/analytics"], TEST_ORG_ID)
    assert principal.permissions == {"workspace:access:view", "workspace:sql:run"} and principal.roles == []
    assert user_row_id == USER_ID and conn_name == "warehouse"


def test_the_run_is_audited_without_the_sql_or_the_token(world):
    _post(world, statements=("insert into secrets values ('hunter2')",))
    (audit,) = world.audits
    assert audit["action"] == "sql_job.run" and audit["user_id"] == USER_ID and audit["resource_id"] == CONN
    assert audit["metadata"] == {"project_id": str(PROJECT), "run_id": str(RUN), "statements": 1, "success": True}
    assert "hunter2" not in json.dumps(audit, default=str)


def test_a_job_that_was_never_bound_cannot_run(world):
    world.db.rows.pop((JobConnectionBinding, PROJECT))
    r = _post(world)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "JOB_NOT_BOUND" and world.ran == []


def test_a_token_for_another_connection_than_the_one_the_job_is_bound_to_is_refused_even_if_that_one_exists(world):
    other = uuid.uuid4()
    world.db.rows[(DatabaseConnection, other)] = _conn(id=other, name="somebody-elses")
    r = _post(world, token=_token(connection_id=str(other)))
    assert r.status_code == 403 and r.json()["detail"]["code"] == "JOB_NOT_BOUND" and world.ran == []
    assert _post(world, token=_token(connection_id=str(uuid.uuid4()))).status_code == 403


def test_a_deleted_connection_or_user_stops_the_job(world):
    world.db.rows.pop((DatabaseConnection, CONN))
    assert _post(world).status_code == 403
    world.db.rows[(DatabaseConnection, CONN)] = _conn()
    world.db.rows.pop((User, USER_ID))
    assert _post(world, token=_token()).status_code == 403 and world.ran == []


def test_a_user_who_may_no_longer_use_the_connection_stops_their_job_before_anything_runs(world):
    world.authz.deny = "no matching grant"
    try:
        r = _post(world)
    finally:
        world.authz.deny = None
    assert r.status_code == 403 and r.json()["detail"]["code"] == "CONNECTION_USE_DENIED"
    assert "no matching grant" in r.json()["detail"]["message"] and world.ran == []


def test_only_postgres_connections_run_jobs(world):
    world.db.rows[(DatabaseConnection, CONN)] = _conn(connection_type="flink")
    r = _post(world)
    assert r.status_code == 400 and r.json()["detail"]["code"] == "UNSUPPORTED_CONNECTION" and world.ran == []


def test_a_token_cannot_be_used_twice(world):
    token = _token()
    assert _post(world, token=token).status_code == 200
    again = _post(world, token=token)
    assert again.status_code == 401 and again.json()["detail"]["code"] == "RUNNER_TOKEN_REPLAYED" and len(world.ran) == 1


def test_a_missing_or_foreign_token_runs_nothing(world):
    assert world.client.post("/api/v1/internal/jobs/sql", json={"statements": ["select 1"]}).status_code == 401
    assert _post(world, token="garbage").status_code == 401
    assert world.ran == []


@pytest.mark.parametrize("body", [{"statements": []}, {"statements": ["x"] * 501}, {"statements": ["select 1"], "timeout_seconds": 0},
                                  {"statements": ["select 1"], "timeout_seconds": 99999}])
def test_malformed_or_oversized_requests_are_refused(world, body):
    r = world.client.post("/api/v1/internal/jobs/sql", json=body, headers={"X-Internal-Service-Token": _token()})
    assert r.status_code == 422 and world.ran == []


def test_an_enormous_script_is_refused_before_the_token_is_spent(world):
    token = _token()
    assert _post(world, token=token, statements=("x" * 1_000_001,)).status_code == 413
    assert _post(world, token=token).status_code == 200                          # the same token still works: it was not consumed


def test_the_endpoint_is_not_in_the_public_api_document(world):
    assert not [p for p in app.openapi()["paths"] if p.startswith("/api/v1/internal")]


# ── registering a job on a connection (the dbt proxy) ────────────────────────────────────────────────────────────────────
@pytest.fixture
def proxy(monkeypatch, redis):
    monkeypatch.setattr(get_settings(), "dbtrunner_delegation_jwt_secret", "dbt-delegation-secret-0123456789abcdef")
    user_row = SimpleNamespace(id=USER_ID)
    db = Db({(DatabaseConnection, CONN): _conn()})
    upstream, authorized, deleted_upstream = [], [], []

    async def fake_proxy(request, path, authorization):
        body = await request.body()
        upstream.append((request.method, path, json.loads(body or b"{}")))
        if request.method == "DELETE":
            return Response(status_code=204)
        return Response(content=json.dumps({"id": str(PROJECT), "kind": "sql"}), status_code=fake_proxy.status, media_type="application/json")
    fake_proxy.status = 201

    async def ensure_user(self, principal):
        return user_row

    async def fake_authorize(principal, user_row_id, conn):
        authorized.append(conn.name)
        if fake_authorize.deny:
            raise ucs_client.UcsAuthorizationError(fake_authorize.deny)
    fake_authorize.deny = None

    class FakeHttp:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def delete(self, url, headers=None):
            deleted_upstream.append(url)
    monkeypatch.setattr(dbt_proxy, "_proxy", fake_proxy)
    monkeypatch.setattr(dbt_proxy, "authorize_connection_use", fake_authorize)
    monkeypatch.setattr(dbt_proxy.httpx, "AsyncClient", FakeHttp)
    monkeypatch.setattr(workspace_service.WorkspaceService, "ensure_user", ensure_user)
    monkeypatch.setattr(dbt_proxy, "authorize_workspace", lambda *a, **k: _none())

    async def _db():
        yield db
    app.dependency_overrides[get_db] = _db
    yield SimpleNamespace(db=db, upstream=upstream, authorized=authorized, fake_proxy=fake_proxy, authz=fake_authorize,
                          deleted=deleted_upstream, client=TestClient(app))
    app.dependency_overrides.clear()


async def _none():
    return None


def _register(p, who=ADMIN, **body):
    payload = {"git_url": "https://github.com/acme/jobs.git", "kind": "sql", "project_path": "jobs/a.sql", **body}
    return p.client.post("/api/v1/dbt/projects/register", json=payload, headers=_headers(**who))


def test_registering_on_a_connection_records_who_it_runs_as_with_their_groups_and_permissions(proxy):
    r = proxy.client.post("/api/v1/dbt/projects/register", headers=_headers(roles=["workspace_admin"], groups=["/finance"]),
                          json={"git_url": "https://github.com/acme/jobs.git", "kind": "sql", "project_path": "jobs/a.sql",
                                "connection_id": str(CONN)})
    assert r.status_code == 201, r.text
    (binding,) = proxy.db.added
    import json
    from role_seed import permissions_for
    assert (binding.project_id, binding.connection_id, binding.user_id) == (PROJECT, CONN, USER_ID)
    assert json.loads(binding.groups) == ["/finance"]
    assert json.loads(binding.permissions) == permissions_for(["workspace_admin"])
    assert not hasattr(binding, "roles")
    assert proxy.authorized == ["warehouse"] and proxy.upstream[0][2]["connection_id"] == str(CONN)


def test_registering_without_a_connection_binds_nothing_and_checks_nothing(proxy):
    r = _register(proxy)
    assert r.status_code == 201 and proxy.db.added == [] and proxy.authorized == []


def test_a_job_the_runner_refused_is_not_bound(proxy):
    proxy.fake_proxy.status = 422
    assert _register(proxy, connection_id=str(CONN)).status_code == 422
    assert proxy.db.added == []


def test_a_caller_who_may_not_run_sql_cannot_create_a_job_on_a_connection(proxy):
    from role_seed import permissions_for
    without_sql = [p for p in permissions_for(["workspace_admin"]) if p != "workspace:sql:run"]
    r = _register(proxy, who={"roles": ["workspace_admin"], "permissions": without_sql}, connection_id=str(CONN))
    assert r.status_code == 403 and proxy.upstream == [] and proxy.db.added == []


def test_a_connection_that_does_not_exist_or_belongs_to_another_org_is_a_404(proxy):
    assert _register(proxy, connection_id=str(uuid.uuid4())).status_code == 404
    proxy.db.rows[(DatabaseConnection, CONN)] = _conn(org_id="another-org")
    assert _register(proxy, connection_id=str(CONN)).status_code == 404
    assert proxy.upstream == []


def test_a_connection_that_cannot_run_jobs_is_refused(proxy):
    proxy.db.rows[(DatabaseConnection, CONN)] = _conn(connection_type="other")
    r = _register(proxy, connection_id=str(CONN))
    assert r.status_code == 400 and "Postgres connections only" in r.json()["detail"]["message"] and proxy.upstream == []


def test_a_caller_who_may_not_use_the_connection_cannot_run_a_job_on_it(proxy):
    proxy.authz.deny = "no matching grant"
    r = _register(proxy, connection_id=str(CONN))
    assert r.status_code == 403 and "no matching grant" in r.json()["detail"]["message"]
    assert proxy.upstream == [] and proxy.db.added == []


def test_a_malformed_connection_id_is_a_validation_error(proxy):
    r = _register(proxy, connection_id="nope")
    assert r.status_code == 400 and r.json()["detail"]["details"]["field"] == "connection_id" and proxy.upstream == []


def test_if_the_binding_cannot_be_saved_the_job_is_taken_back_from_the_runner(proxy, monkeypatch):
    async def boom():
        raise RuntimeError("db down")
    proxy.db.flush = boom
    with pytest.raises(RuntimeError):
        _register(proxy, connection_id=str(CONN))
    assert proxy.deleted == [f"{get_settings().dbtrunner_url}/api/v1/projects/{PROJECT}"]


def test_deleting_a_job_removes_its_binding(proxy):
    r = proxy.client.delete(f"/api/v1/dbt/projects/{PROJECT}", headers=_headers(**ADMIN))
    assert r.status_code == 204 and len(proxy.db.executed) == 1
    assert "job_connection_bindings" in str(proxy.db.executed[0])


def test_a_binding_that_cannot_be_removed_does_not_fail_the_delete_the_runner_already_did(proxy):
    proxy.db.fail_execute = True
    r = proxy.client.delete(f"/api/v1/dbt/projects/{PROJECT}", headers=_headers(**ADMIN))
    assert r.status_code == 204 and proxy.db.rolled_back == 1


# ── the executor, against a real Postgres ────────────────────────────────────────────────────────────────────────────────
PG_URL = os.environ.get("TEST_PG_URL")      # e.g. postgresql://test:test@dgx-testdb:5432/dbtrunner_test
needs_pg = pytest.mark.skipif(not PG_URL, reason="needs a throwaway Postgres: set TEST_PG_URL")


@pytest.fixture
async def pg(monkeypatch):
    from urllib.parse import urlparse
    u = urlparse(PG_URL)
    monkeypatch.setattr(cje, "assert_host_allowed", _allow)
    monkeypatch.setattr("app.api.catalog._load_db_creds", _creds(u))
    import asyncpg
    admin = await asyncpg.connect(PG_URL)
    yield SimpleNamespace(conn=_conn(host=u.hostname, port=u.port, database_name=u.path.lstrip("/")), admin=admin, password=u.password)
    await admin.close()


async def _allow(host):
    return None


def _creds(u):
    async def load(path):
        return {"username": u.username, "password": u.password}
    return load


def _table() -> str:
    return f"wsjob_{uuid.uuid4().hex[:10]}"


@needs_pg
async def test_every_statement_runs_and_commits_together(pg):
    t = _table()
    try:
        result = await cje.run_job_statements(conn=pg.conn, timeout_seconds=30, statements=[
            f"create table {t} (id int)", f"insert into {t} values (1), (2), (3)", f"update {t} set id = id + 10 where id > 1"])
        assert result["success"] is True
        assert [(n["node_id"], n["status"], n["message"]) for n in result["nodes"]] == [
            ("statement.1", "success", "CREATE"), ("statement.2", "success", "INSERT · 3 rows"), ("statement.3", "success", "UPDATE · 2 rows")]
        assert sorted(r[0] for r in await pg.admin.fetch(f"select id from {t}")) == [1, 12, 13]
    finally:
        await pg.admin.execute(f"drop table if exists {t}")


@needs_pg
async def test_a_failing_statement_rolls_everything_back_and_skips_the_rest(pg):
    t = _table()
    result = await cje.run_job_statements(conn=pg.conn, timeout_seconds=30, statements=[
        f"create table {t} (id int)", f"insert into {t} values (1)", "insert into no_such_table values (1)", f"insert into {t} values (2)"])
    assert result["success"] is False
    assert [n["status"] for n in result["nodes"]] == ["success", "success", "error", "skipped"]
    assert 'relation "no_such_table" does not exist' in result["nodes"][2]["message"]
    assert result["errors"][0].startswith("statement 3 failed, nothing was committed:")
    assert await pg.admin.fetchval(f"select to_regclass('public.{t}')") is None            # the CREATE was undone too


@needs_pg
async def test_a_slow_statement_is_stopped_at_the_timeout(pg):
    result = await cje.run_job_statements(conn=pg.conn, timeout_seconds=1, statements=["select pg_sleep(5)"])
    assert result["success"] is False and result["nodes"][0]["status"] == "error"


@needs_pg
async def test_the_password_is_hidden_even_when_the_database_prints_it(pg):
    """The throwaway database's password is "test": a statement that raises an error containing it must not leak it."""
    result = await cje.run_job_statements(conn=pg.conn, timeout_seconds=30, statements=[
        f"do $$ begin raise exception 'the password is {pg.password}'; end $$"])
    reported = json.dumps([result["errors"], result["nodes"][0]["message"]])      # the statement's own text is the author's, not a leak
    assert result["success"] is False and pg.password not in reported and "the password is ***" in reported


@needs_pg
async def test_the_password_never_appears_in_an_error(pg, monkeypatch):
    monkeypatch.setattr(cje, "assert_host_allowed", _allow)
    bad = _conn(host=pg.conn.host, port=pg.conn.port, database_name="no_such_database_" + uuid.uuid4().hex[:6])
    result = await cje.run_job_statements(conn=bad, timeout_seconds=5, statements=["select 1"])
    assert result["success"] is False and pg.password not in json.dumps(result)


async def test_an_unsupported_connection_type_or_a_blocked_host_runs_nothing(monkeypatch):
    out = await cje.run_job_statements(conn=_conn(connection_type="flink"), statements=["select 1"], timeout_seconds=5)
    assert out["success"] is False and "Postgres connections only" in out["errors"][0]

    async def blocked(host):
        raise cje.EgressPolicyError("not on the allowlist")
    monkeypatch.setattr(cje, "assert_host_allowed", blocked)
    out = await cje.run_job_statements(conn=_conn(), statements=["select 1"], timeout_seconds=5)
    assert out["success"] is False and "Host not permitted" in out["errors"][0]
