"""The codegen assistant is grounded on the connection the user is working against.

It used to see only the generic lakehouse catalog (which does not contain the user's database), so "retrieve all rows from dim_customers" got
invented columns (`customer_name`, `email`, `phone`, ...) and the query failed with `column "customer_name" does not exist`. Now the turn carries
the editor's connection and the grounding pack holds that connection's REAL tables and columns, read through the Object Explorer's catalog adapter.
"""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from app.core.auth import Principal
from app.schemas.catalog import CatalogColumn, CatalogNamespace, CatalogTable
from app.schemas.codegen import CodegenPromptRequest
from app.services import codegen as cg

PRINCIPAL = Principal(user_id="u", org_id="o", roles=[], email="u@x", issued_at=0, expires_at=0)
CONN_ID = uuid.uuid4()
DIM_CUSTOMERS = ["customer_id", "full_name", "email", "city", "signup_date", "lifetime_value", "customer_segment"]


def run(coro):
    return asyncio.run(coro)


class FakeAdapter:
    """What the Object Explorer's catalog adapter returns for a Postgres connection."""

    def __init__(self, *, fail=False):
        self.fail, self.calls = fail, []
        self.tables = {"public": ["dim_customers", "fct_orders", "customers", "exec_summary"], "snapshots": ["orders_snapshot"],
                       "information_schema": ["columns"]}
        self.columns = {"dim_customers": DIM_CUSTOMERS, "fct_orders": ["order_id", "customer_id", "order_total"],
                        "customers": ["customer_id", "name"], "exec_summary": ["metric", "value"], "orders_snapshot": ["order_id"]}

    async def list_namespaces(self, conn, principal):
        self.calls.append("namespaces")
        if self.fail:
            raise RuntimeError("catalog down")
        return [CatalogNamespace(name=n) for n in self.tables]

    async def list_tables(self, conn, ns, principal):
        self.calls.append(f"tables:{ns}")
        return [CatalogTable(name=t, schema_name=ns) for t in self.tables[ns]]

    async def list_columns(self, conn, ns, table, principal):
        self.calls.append(f"columns:{ns}.{table}")
        return [CatalogColumn(name=c, data_type="text") for c in self.columns[table]]


@pytest.fixture
def wired(monkeypatch):
    adapter = FakeAdapter()
    conn = SimpleNamespace(id=CONN_ID)

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, model, key):
            return conn if key == CONN_ID else None

        def expunge(self, obj):
            pass

    monkeypatch.setattr("app.db.session.AsyncSessionLocal", lambda: Session())
    monkeypatch.setattr("app.services.catalog_adapter.get_catalog_adapter", lambda: adapter)
    return adapter


# ── what is described to the model ───────────────────────────────────────────────────────
def test_the_table_the_prompt_names_is_described_first():
    pairs = [("public", "customers"), ("public", "dim_customers"), ("public", "fct_orders")]
    assert cg.rank_connection_tables(pairs, "Give query for retrive all rows from dim_customers table")[0] == ("public", "dim_customers")


def test_a_schema_qualified_name_in_the_prompt_wins():
    pairs = [("public", "orders"), ("snapshots", "orders")]
    assert cg.rank_connection_tables(pairs, "rows from snapshots.orders")[0] == ("snapshots", "orders")


def test_with_no_match_the_user_schema_comes_first_and_the_list_is_bounded():
    pairs = [("zeta", f"t{i}") for i in range(30)] + [("public", "a")]
    ranked = cg.rank_connection_tables(pairs, "something unrelated")
    assert ranked[0] == ("public", "a") and len(ranked) == 8


def test_the_real_columns_of_the_named_table_reach_the_pack(wired):
    tables = run(cg.ground_from_connection(CONN_ID, PRINCIPAL, "retrieve all rows from dim_customers"))
    dim = next(t for t in tables if t.fqn == "public.dim_customers")
    assert [c.name for c in dim.columns] == DIM_CUSTOMERS
    assert "customer_name" not in [c.name for t in tables for c in t.columns]            # the column the model used to invent


def test_system_schemas_are_never_described(wired):
    tables = run(cg.ground_from_connection(CONN_ID, PRINCIPAL, "columns"))
    assert not any(t.fqn.startswith("information_schema.") for t in tables)
    assert "tables:information_schema" not in wired.calls


def test_an_unknown_connection_gives_no_tables(wired):
    assert run(cg.ground_from_connection(uuid.uuid4(), PRINCIPAL, "x")) == []


def test_a_catalog_failure_gives_no_tables_instead_of_breaking_the_turn(monkeypatch, wired):
    wired.fail = True
    assert run(cg.ground_from_connection(CONN_ID, PRINCIPAL, "dim_customers")) == []


def test_one_unreadable_table_does_not_hide_the_others(wired):
    real = wired.list_columns

    async def flaky(conn, ns, table, principal):
        if table == "fct_orders":
            raise RuntimeError("no grant")
        return await real(conn, ns, table, principal)
    wired.list_columns = flaky
    names = {t.fqn for t in run(cg.ground_from_connection(CONN_ID, PRINCIPAL, "dim_customers fct_orders"))}
    assert "public.dim_customers" in names and "public.fct_orders" not in names


# ── the grounder the harness calls ───────────────────────────────────────────────────────
class _Backend:
    def __init__(self):
        self.listed = 0

    async def list_sources(self, *, namespace):
        self.listed += 1
        return {"tables": ["lake.only_table"]}

    async def get_table_schema(self, *, table):
        return {"columns": [{"name": "lake_col", "type": "int"}]}


def _ground(connection_id, principal, prompt="rows from dim_customers"):
    backend = _Backend()
    ground = cg.CodegenService.__new__(cg.CodegenService)._make_grounder(backend, connection_id=connection_id, principal=principal)
    return backend, run(ground(prompt, "sql", []))


def test_the_pack_comes_from_the_connection_not_the_lakehouse_when_the_connection_has_tables(wired):
    backend, pack = _ground(CONN_ID, PRINCIPAL)
    assert any(t.fqn == "public.dim_customers" for t in pack.tables)
    assert backend.listed == 0 and not any(t.fqn == "lake.only_table" for t in pack.tables)


def test_it_falls_back_to_the_lakehouse_catalog_when_the_connection_gives_nothing(wired):
    wired.fail = True
    backend, pack = _ground(CONN_ID, PRINCIPAL)
    assert backend.listed == 1 and [t.fqn for t in pack.tables] == ["lake.only_table"]


def test_without_a_connection_the_old_behaviour_is_unchanged(wired):
    backend, pack = _ground(None, None)
    assert backend.listed == 1 and wired.calls == []


# ── the request ─────────────────────────────────────────────────────────────────────────
def test_the_prompt_request_accepts_the_editors_connection():
    req = CodegenPromptRequest(prompt="hi", ws_session_id=uuid.uuid4(), connection_id=CONN_ID)
    assert req.connection_id == CONN_ID
    assert CodegenPromptRequest(prompt="hi", ws_session_id=uuid.uuid4()).connection_id is None


# ── a validator that is not there is "skipped", not "failed" ──────────────────────────────
class _Resp:
    def __init__(self, status, body=None, ctype="application/json"):
        self.status_code, self._body, self.headers, self.text = status, body or {}, {"content-type": ctype}, str(body)

    def json(self):
        return self._body


def _explain(monkeypatch, *, resp=None, exc=None):
    class Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

        async def post(self, url, json=None):
            if exc:
                raise exc
            return resp
    monkeypatch.setattr(cg.httpx, "AsyncClient", Client)
    return run(cg.SqlBackend()._lakehouse_explain("select 1"))


@pytest.mark.parametrize("status", [404, 405, 501, 500, 502, 503])
def test_an_explain_service_that_cannot_answer_skips_validation(monkeypatch, status):
    out = _explain(monkeypatch, resp=_Resp(status, {"detail": "Not Found"}))
    assert out["ok"] is True and out["skipped"] is True and str(status) in out["reason"]


def test_an_unreachable_explain_service_skips_validation(monkeypatch):
    import httpx
    out = _explain(monkeypatch, exc=httpx.ConnectError("refused"))
    assert out["ok"] is True and out["skipped"] is True


@pytest.mark.parametrize("status", [400, 422])
def test_a_real_sql_error_still_fails_validation(monkeypatch, status):
    out = _explain(monkeypatch, resp=_Resp(status, {"message": 'column "x" does not exist'}))
    assert out == {"ok": False, "errors": ['column "x" does not exist']}


def test_a_valid_statement_passes(monkeypatch):
    assert _explain(monkeypatch, resp=_Resp(200, {})) == {"ok": True, "errors": []}
