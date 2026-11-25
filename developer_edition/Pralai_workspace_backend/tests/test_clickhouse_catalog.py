"""ClickHouse for the code assistant: where its table context comes from and how a candidate is checked.

The assistant reads table and column names from ClickHouse's HTTP interface (`system.tables` / `system.columns`, metadata only) and checks a
candidate with EXPLAIN (analysed, never run), always with `readonly=1`. Not configured, or a server that cannot answer, is said plainly or
skipped; it is never reported as a mistake in the SQL.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from app.core.config import get_settings
from app.services import clickhouse_catalog as ch
from app.services import codegen as cg


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def cfg(monkeypatch):
    s = get_settings()
    for k, v in dict(clickhouse_url="http://clickhouse:8123", clickhouse_user="inaiera", clickhouse_password="pw",
                     clickhouse_database="analytics", clickhouse_timeout_seconds=5).items():
        monkeypatch.setattr(s, k, v)
    return s


class _Server:
    """A stand-in ClickHouse: records every request, answers with the next canned (status, body)."""
    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status, body = self.answers.pop(0) if self.answers else (200, "")
        return httpx.Response(status, text=body)

    def install(self, monkeypatch):
        real = httpx.AsyncClient
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(self.handler), **kw))
        return self

    @property
    def last_query(self) -> str:
        return self.requests[-1].content.decode()


def test_without_a_url_the_assistant_says_clickhouse_is_not_configured(monkeypatch):
    monkeypatch.setattr(get_settings(), "clickhouse_url", None)
    with pytest.raises(ch.ClickHouseNotConfigured, match="CLICKHOUSE_URL"):
        ch.ClickHouseCatalog()


def test_every_request_is_read_only_and_signed_in_as_the_configured_user(cfg, monkeypatch):
    server = _Server((200, "")).install(monkeypatch)
    run(ch.ClickHouseCatalog().list_tables())
    req = server.requests[0]
    assert req.url.params["readonly"] == "1" and req.url.params["database"] == "analytics"
    assert req.headers["authorization"].startswith("Basic ") and str(req.url).startswith("http://clickhouse:8123/")


def test_tables_come_from_user_databases_only(cfg, monkeypatch):
    server = _Server((200, '{"database":"sales","name":"orders","comment":"one row per order"}\n{"database":"sales","name":"items","comment":""}\n')).install(monkeypatch)
    listing = run(ch.ClickHouseCatalog().list_tables())
    assert listing == {"tables": [{"fqn": "sales.orders", "description": "one row per order"}, {"fqn": "sales.items", "description": None}]}
    assert "system.tables" in server.last_query and "NOT IN ('system', 'INFORMATION_SCHEMA', 'information_schema')" in server.last_query


def test_a_tables_columns_nullability_and_partition_key(cfg, monkeypatch):
    server = _Server((200, '{"name":"id","type":"UInt64","is_in_partition_key":0}\n'
                           '{"name":"note","type":"Nullable(String)","is_in_partition_key":0}\n'
                           '{"name":"day","type":"Date","is_in_partition_key":1}\n')).install(monkeypatch)
    schema = run(ch.ClickHouseCatalog().table_schema("sales.orders"))
    assert schema["columns"] == [{"name": "id", "type": "UInt64", "nullable": False}, {"name": "note", "type": "Nullable(String)", "nullable": True},
                                 {"name": "day", "type": "Date", "nullable": False}]
    assert schema["partition_keys"] == ["day"]
    assert "database = 'sales' AND table = 'orders'" in server.last_query


def test_a_bare_table_name_uses_the_configured_database(cfg, monkeypatch):
    server = _Server((200, "")).install(monkeypatch)
    run(ch.ClickHouseCatalog().table_schema("orders"))
    assert "database = 'analytics' AND table = 'orders'" in server.last_query


def test_a_quote_in_a_table_name_cannot_break_out_of_the_query(cfg, monkeypatch):
    server = _Server((200, "")).install(monkeypatch)
    run(ch.ClickHouseCatalog().table_schema("db.x' OR '1'='1"))
    assert "table = 'x\\' OR \\'1\\'=\\'1'" in server.last_query


@pytest.mark.parametrize("sql,kind", [
    ("SELECT 1", "PLAN"), ("  with a as (select 1) select * from a", "PLAN"), ("(select 1) union all (select 2)", "PLAN"),
    ("CREATE TABLE t (x Int8) ENGINE = Memory", "AST"), ("INSERT INTO t SELECT 1", "AST"),
])
def test_a_select_is_analysed_and_anything_else_is_parse_checked(cfg, monkeypatch, sql, kind):
    server = _Server((200, "")).install(monkeypatch)
    assert run(ch.ClickHouseCatalog().explain(sql)) == {"ok": True, "errors": []}
    assert server.last_query.startswith(f"EXPLAIN {kind} ")


def test_only_the_first_statement_is_checked_and_a_trailing_semicolon_is_dropped(cfg, monkeypatch):
    server = _Server((200, "")).install(monkeypatch)
    run(ch.ClickHouseCatalog().explain("select 1; drop table t;"))
    assert server.last_query == "EXPLAIN PLAN select 1"


def test_the_servers_message_is_the_error_when_the_sql_is_wrong(cfg, monkeypatch):
    _Server((400, "Code: 47. DB::Exception: Unknown identifier nope\n")).install(monkeypatch)
    out = run(ch.ClickHouseCatalog().explain("select nope from t"))
    assert out["ok"] is False and out["errors"] == ["Code: 47. DB::Exception: Unknown identifier nope"]


@pytest.mark.parametrize("status", [401, 403, 502, 503])
def test_a_server_that_cannot_answer_skips_the_check_instead_of_failing_the_sql(cfg, monkeypatch, status):
    _Server((status, "denied")).install(monkeypatch)
    out = run(ch.ClickHouseCatalog().explain("select 1"))
    assert out["ok"] is True and out["skipped"] is True and out["errors"] == []


def test_an_unreachable_server_skips_the_check(cfg, monkeypatch):
    def boom(request):
        raise httpx.ConnectError("refused")
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(boom), **kw))
    out = run(ch.ClickHouseCatalog().explain("select 1"))
    assert out["ok"] is True and out["skipped"] is True and "unreachable" in out["reason"]


# ── wired into a turn ────────────────────────────────────────────────────────────────────────────────────────────────────
def test_the_sql_check_for_clickhouse_goes_to_clickhouse_and_not_to_the_lakehouse(cfg, monkeypatch):
    server = _Server((200, "")).install(monkeypatch)
    out = run(cg.get_backend("sql").compile_code(code="select 1", path="q.sql", companion_code=None, engine="clickhouse"))
    assert out == {"ok": True, "errors": []} and server.requests[0].url.host == "clickhouse"


def test_the_sql_check_for_trino_still_goes_to_the_lakehouse(cfg, monkeypatch):
    server = _Server((200, "{}")).install(monkeypatch)
    run(cg.get_backend("sql").compile_code(code="select 1", path="q.sql", companion_code=None, engine="trino"))
    assert server.requests[0].url.path == "/v1/queries/explain" and server.requests[0].url.host != "clickhouse"


class _LakeBackend:
    async def list_sources(self, *, namespace):
        return {"tables": ["lake.only_table"]}

    async def get_table_schema(self, *, table):
        return {"columns": [{"name": "lake_col", "type": "int"}]}


def _ground(engine, prompt="revenue by day from orders", target="sql"):
    ground = cg.CodegenService.__new__(cg.CodegenService)._make_grounder(_LakeBackend(), engine=engine)
    return run(ground(prompt, target, []))


def test_a_clickhouse_turn_is_grounded_in_clickhouse_tables_not_the_lakehouse(cfg, monkeypatch):
    _Server((200, '{"database":"sales","name":"orders","comment":""}\n'),
            (200, '{"name":"id","type":"UInt64","is_in_partition_key":0}\n{"name":"day","type":"Date","is_in_partition_key":1}\n')).install(monkeypatch)
    pack = _ground("clickhouse")
    assert [t.fqn for t in pack.tables] == ["sales.orders"]
    assert [(c.name, c.type) for c in pack.tables[0].columns] == [("id", "UInt64"), ("day", "Date")] and pack.tables[0].partition_keys == ["day"]


def test_a_clickhouse_turn_without_clickhouse_configured_gets_an_empty_pack_not_lakehouse_tables(monkeypatch):
    monkeypatch.setattr(get_settings(), "clickhouse_url", None)
    assert _ground("clickhouse").tables == []


@pytest.mark.parametrize("engine,target", [("trino", "sql"), (None, "sql"), ("pg_lake", "dbt"), ("clickhouse", "python")])
def test_every_other_turn_is_still_grounded_in_the_lakehouse_catalog(cfg, engine, target):
    assert [t.fqn for t in _ground(engine, target=target).tables] == ["lake.only_table"]
