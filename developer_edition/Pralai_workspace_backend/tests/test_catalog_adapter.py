"""CatalogAdapter: linking a connection to its UCS catalog, and failing with
the real reason instead of fabricated placeholder data (WS-CAT-008)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.services import catalog_adapter as ca
from app.services.catalog_adapter import CatalogUnavailableError, match_ucs_catalog


def _rel(host, port, database, user="ucs"):
    return {"engine": "relational", "schemas": {},
            "connection": {"host": host, "port": port, "database": database, "user": user}}


TREE = {
    "pg_sales": _rel("postgres", 5432, "sales"),
    "pg_demo": _rel("postgres", 5432, "sales"),
    "dbt_lake": _rel("host.docker.internal", 5433, "dbtrunner", user="dbtrunner"),
    "lake": {"engine": "iceberg", "schemas": {}},
}


def _match(**kw):
    args = dict(connection_type="postgres", host="host.docker.internal", port=5432,
                database="sales", username="ucs", dev_mode=True)
    args.update(kw)
    return match_ucs_catalog(TREE, **args)


def test_same_host_wins():
    assert _match(host="host.docker.internal", port=5433, database="dbtrunner", username=None) == "dbt_lake"


def test_dev_tolerates_docker_hostname_when_target_is_unique():
    # Both candidates point at the same postgres:5432/sales → deterministic pick.
    assert _match() == "pg_demo"


def test_prod_requires_host_match():
    assert _match(dev_mode=False) is None


def test_database_and_port_must_match():
    assert _match(database="other") is None
    assert _match(port=6543) is None


def test_user_must_match_without_host():
    assert _match(username="someone_else") is None


def test_two_distinct_targets_are_ambiguous():
    tree = {"a": _rel("db1", 5432, "sales"), "b": _rel("db2", 5432, "sales")}
    assert match_ucs_catalog(tree, connection_type="postgres", host="other", port=5432,
                             database="sales", username="ucs", dev_mode=True) is None


def test_non_postgres_never_auto_matches():
    assert _match(connection_type="trino") is None


def _conn(**kw):
    base = dict(id="c1", name="sales", connection_type="postgres", host="host.docker.internal",
                port=5432, database_name="sales", catalog_name=None, vault_secret_path=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_linked_catalog_is_browsed_through_ucs(monkeypatch):
    tree = {"pg_sales": {**_rel("host.docker.internal", 5432, "sales"),
                         "schemas": {"finance": [{"name": "invoices", "type": "table"}]}}}

    async def _tree(_p):
        return tree
    monkeypatch.setattr(ca, "get_explorer_tree", _tree)
    adapter = ca.CatalogAdapter()
    ns = asyncio.run(adapter.list_namespaces(_conn(), principal=None))
    assert [n.name for n in ns] == ["finance"]
    tables = asyncio.run(adapter.list_tables(_conn(), "finance", principal=None))
    assert [(t.name, t.catalog_name) for t in tables] == [("invoices", "pg_sales")]


@pytest.mark.parametrize("env", ["dev", "prod"])
def test_no_placeholder_data_when_nothing_answers(monkeypatch, env):
    async def _tree(_p):
        return {}

    async def _boom(*_a, **_k):
        raise OSError('PostgreSQL server at "x:5432" rejected SSL upgrade')
    monkeypatch.setattr(ca, "get_explorer_tree", _tree)
    monkeypatch.setattr(ca, "assert_host_allowed", lambda _h: asyncio.sleep(0))
    monkeypatch.setattr(ca.CatalogAdapter, "_dev_mode", lambda self: env == "dev")
    import app.api.catalog as api
    monkeypatch.setattr(api, "_pg_catalog_query", _boom)

    with pytest.raises(CatalogUnavailableError) as ei:
        asyncio.run(ca.CatalogAdapter().list_namespaces(_conn(), principal=None))
    if env == "dev":
        assert "turn off" in ei.value.reason          # actionable TLS hint
    else:
        assert "Unified Catalog" in ei.value.reason


def test_gravitino_object_column_types():
    assert ca._type_name({"type": "external", "catalogString": "bigserial"}) == "bigserial"
    assert ca._type_name("integer") == "integer"
    assert ca._type_name(None) == "unknown"
