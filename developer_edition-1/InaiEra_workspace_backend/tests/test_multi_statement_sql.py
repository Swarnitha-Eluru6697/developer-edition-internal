"""A SQL script of several statements returns EVERY statement's result, in order.

Before, only the last statement's rows were streamed and the others were run and discarded, so
`select * from a; select * from b;` showed just b. Chunks now carry a `statement` index.
Uses a fake asyncpg connection, so no database is needed.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import asyncpg
import pytest

from app.api.executions import assemble_sql_outputs
from app.executors import connection_sql_executor as cse
from app.executors.connection_sql_executor import ConnectionSqlExecutor, split_statements
from app.executors.sql_executor import SqlChunk, SqlExecutor


# ---- fake asyncpg --------------------------------------------------------------------------
class _Attr:
    def __init__(self, name, typ="int4"):
        self.name = name
        self.type = SimpleNamespace(name=typ)


class _Row:
    def __init__(self, values):
        self._v = values

    def values(self):
        return self._v


class _Cursor:
    def __init__(self, rows):
        self._rows = list(rows)

    async def fetch(self, n):
        out, self._rows = self._rows[:n], self._rows[n:]
        return [_Row(r) for r in out]


class _Prepared:
    def __init__(self, attrs, rows, status, error):
        self._attrs, self._rows, self._status, self._error = attrs, rows, status, error

    def get_attributes(self):
        return self._attrs

    async def fetch(self):
        if self._error:
            raise self._error
        return []

    def get_statusmsg(self):
        return self._status

    async def cursor(self):
        return _Cursor(self._rows)


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeConn:
    """script: {sql text: ("rows", [attr names], [[row], ...]) | ("status", "INSERT 0 3") | ("error", Exc)}"""

    def __init__(self, script):
        self.script, self.prepared = script, []

    async def prepare(self, sql):
        self.prepared.append(sql)
        kind, *rest = self.script[sql]
        if kind == "rows":
            return _Prepared([_Attr(n) for n in rest[0]], rest[1], None, None)
        if kind == "status":
            return _Prepared([], [], rest[0], None)
        return _Prepared([], [], None, rest[0])

    def transaction(self, **_):
        return _Tx()

    async def close(self, timeout=None):
        pass

    def terminate(self):
        pass


@pytest.fixture
def run(monkeypatch):
    async def _no_check(host):
        return None

    async def _creds(path):
        return {"username": "u", "password": ""}

    monkeypatch.setattr(cse, "assert_host_allowed", _no_check)
    monkeypatch.setattr("app.api.catalog._load_db_creds", _creds)
    conn = SimpleNamespace(connection_type="postgres", host="h", port=1, database_name="d",
                           use_tls=False, vault_secret_path="x")

    def go(script, sql, max_rows=100, chunk_size=2):
        fake = FakeConn(script)

        async def _connect(**_):
            return fake

        monkeypatch.setattr(asyncpg, "connect", _connect)

        async def collect():
            return [c async for c in ConnectionSqlExecutor().execute(
                conn=conn, code_text=sql, max_rows=max_rows, timeout_seconds=30, chunk_size=chunk_size)]
        return asyncio.run(collect()), fake
    return go


def _kinds(chunks):
    return [(c.output_type, c.payload.get("statement")) for c in chunks]


# ---- ConnectionSqlExecutor -----------------------------------------------------------------
def test_every_select_streams_its_own_result(run):
    script = {"select * from a": ("rows", ["id"], [[1], [2], [3]]),
              "select * from b": ("rows", ["name"], [["x"]])}
    chunks, fake = run(script, "select * from a;\n\nselect * from b;")
    assert fake.prepared == ["select * from a", "select * from b"]
    assert _kinds(chunks) == [
        ("rows", 0), ("rows", 0), ("statement", 0),      # a: 3 rows in chunks of 2
        ("rows", 1), ("statement", 1),
        ("done", None),
    ]
    first = chunks[0].payload
    assert first["columns"] == [{"name": "id", "type": "int4"}] and first["sql"] == "select * from a"
    assert chunks[3].payload["columns"] == [{"name": "name", "type": "int4"}]
    assert chunks[2].payload["total_rows"] == 3 and chunks[-1].is_final and chunks[-1].payload["statements"] == 2
    assert [c for c in chunks if c.is_final] == [chunks[-1]]          # only the end closes the stream


def test_a_statement_without_rows_reports_its_status(run):
    script = {"create table t (id int)": ("status", "CREATE TABLE"), "select * from t": ("rows", ["id"], [])}
    chunks, _ = run(script, "create table t (id int); select * from t")
    assert chunks[0].output_type == "statement" and chunks[0].payload["status"] == "CREATE TABLE"
    # a SELECT that returns no rows still tells the UI its columns
    assert chunks[1].output_type == "rows" and chunks[1].payload["rows"] == [] and chunks[1].payload["columns"]


def test_a_failing_statement_stops_the_script_and_names_itself(run):
    script = {"select 1": ("rows", ["a"], [[1]]),
              "select * from nope": ("error", asyncpg.PostgresError("relation does not exist")),
              "select 3": ("rows", ["c"], [[3]])}
    chunks, fake = run(script, "select 1; select * from nope; select 3")
    assert fake.prepared == ["select 1", "select * from nope"]        # the third never ran
    err = chunks[-1]
    assert err.output_type == "error" and err.is_final and err.payload["statement"] == 1
    assert ("rows", 0) in _kinds(chunks)                               # the first result is kept


def test_the_row_cap_applies_per_statement(run):
    script = {"select * from a": ("rows", ["id"], [[i] for i in range(10)])}
    chunks, _ = run(script, "select * from a", max_rows=4)
    closing = next(c for c in chunks if c.output_type == "statement")
    assert closing.payload["total_rows"] == 4 and closing.payload["truncated"] is True


def test_split_statements_is_shared_and_respects_strings_and_comments():
    assert split_statements("select 1; -- c\nselect ';' ; /* x; */ select \"a;b\"") == ["select 1", "select ';'", 'select "a;b"']
    from app.executors.sql_split import split_statements as shared
    assert shared is split_statements


# ---- SqlExecutor (lakehouse) ---------------------------------------------------------------
def test_lakehouse_script_runs_per_statement_and_tags_results(monkeypatch):
    seen = []

    async def fake_stream_one(self, *, sql, statement, **_):
        seen.append((sql, statement))
        if "bad" in sql:
            yield SqlChunk("error", {"error_code": "QUERY_ERROR", "error_message": "boom"}, 0, True)
            return
        yield SqlChunk("rows", {"rows": [[1]], "columns": [{"name": "n", "type": "int"}]}, 1, False)
        yield SqlChunk("done", {"total_rows": 1}, 0, True)

    monkeypatch.setattr(SqlExecutor, "_stream_one", fake_stream_one)

    async def collect(code):
        return [c async for c in SqlExecutor().execute(
            execution_id="e", user_id="u", code_text=code, target=None, max_rows=10, timeout_seconds=5)]

    chunks = asyncio.run(collect("select 1; select 2"))
    assert seen == [("select 1", 0), ("select 2", 1)]
    assert _kinds(chunks) == [("rows", 0), ("statement", 0), ("rows", 1), ("statement", 1), ("done", None)]
    assert [c for c in chunks if c.is_final] == [chunks[-1]]

    seen.clear()
    single = asyncio.run(collect("select 1"))
    assert seen == [("select 1", None)] and _kinds(single) == [("rows", None), ("done", None)]  # untouched path

    stopped = asyncio.run(collect("select 1; bad; select 3"))
    assert stopped[-1].output_type == "error" and stopped[-1].payload["statement"] == 1
    assert ("select 3", 2) not in seen


# ---- stored result -------------------------------------------------------------------------
def _out(output_type, payload):
    return SimpleNamespace(output_type=output_type, payload=payload)


def test_stored_result_groups_rows_by_statement_and_keeps_the_old_top_level():
    stored = [
        _out("rows", {"rows": [[1], [2]], "columns": [{"name": "id"}], "statement": 0, "sql": "select * from a"}),
        _out("statement", {"statement": 0, "sql": "select * from a", "total_rows": 2}),
        _out("statement", {"statement": 1, "sql": "insert into t values (1)", "total_rows": 0, "status": "INSERT 0 1"}),
        _out("rows", {"rows": [["x"]], "columns": [{"name": "name"}], "statement": 2, "sql": "select * from b"}),
        _out("statement", {"statement": 2, "sql": "select * from b", "total_rows": 1}),
    ]
    columns, rows, _outputs, results = assemble_sql_outputs(stored)
    assert [r["statement"] for r in results] == [0, 1, 2]
    assert results[0]["rows"] == [[1], [2]] and results[2]["rows"] == [["x"]]
    assert results[1]["columns"] == [] and results[1]["status"] == "INSERT 0 1"
    # one-result clients still see the last result set that has columns
    assert columns == [{"name": "name"}] and rows == [["x"]]


def test_stored_single_statement_is_unchanged_and_old_chunks_without_an_index_still_work():
    columns, rows, outputs, results = assemble_sql_outputs([
        _out("rows", {"rows": [[1]], "columns": [{"name": "id"}]}),
        _out("rows", {"rows": [[2]]}),
        _out("done", {"total_rows": 2}),
    ])
    assert columns == [{"name": "id"}] and rows == [[1], [2]] and outputs == [] and len(results) == 1


def test_a_failed_statement_error_is_recorded_on_its_result():
    _c, _r, outputs, results = assemble_sql_outputs([
        _out("rows", {"rows": [[1]], "columns": [{"name": "a"}], "statement": 0}),
        _out("error", {"error_code": "QUERY_ERROR", "error_message": "boom", "statement": 1}),
    ])
    assert results[1]["error"] == "boom" and outputs[0]["output_type"] == "error"
