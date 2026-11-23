"""
GAP-10: connection discovery's result now lives as DiscoveryRun (a small,
pollable summary) + DiscoveryObject rows (paged through, not one JSON
blob) instead of inside Operation.result — see
app/services/discovery_service.py and app/models/discovery.py.
"""

import pytest
from fastapi import HTTPException

import app.services.discovery_service as discovery_service_module
from app.services.discovery_service import discovery_service

CONNECTION = {
    "id": "conn-1", "org_id": "org-1", "source_type": "postgres",
    "connection_config": {"hostname": "db.internal", "database": "erp"},
}


@pytest.mark.asyncio
async def test_run_discovery_persists_schemas_and_tables_as_objects(monkeypatch, fake_postgres) -> None:
    run = await discovery_service.create_run("org-1", "conn-1")

    async def _fake_discover(connection, timeout=8.0, *, org_id=None):
        return {
            "connected": True, "message": "Found 3 tables across 2 schemas",
            "schemas": ["public", "sales"],
            "tables": [
                {"schema": "public", "name": "customers"},
                {"schema": "public", "name": "orders"},
                {"schema": "sales", "name": "leads"},
            ],
            "objects": [],
        }

    monkeypatch.setattr(discovery_service_module, "discover", _fake_discover)

    await discovery_service.run_discovery("org-1", run["id"], CONNECTION)

    updated = await discovery_service.get_run("org-1", run["id"])
    assert updated["status"] == "succeeded"
    assert updated["object_count"] == 5  # 2 schemas + 3 tables
    assert updated["error_message"] is None
    assert updated["completed_at"] is not None

    objects, next_cursor = await discovery_service.list_objects(
        "org-1", run["id"], search=None, limit=100, cursor=None,
    )
    assert next_cursor is None
    assert len(objects) == 5
    assert {"public", "sales"} <= {o["name"] for o in objects if o["object_type"] == "schema"}
    table_names = {o["name"] for o in objects if o["object_type"] == "table"}
    assert table_names == {"customers", "orders", "leads"}


@pytest.mark.asyncio
async def test_run_discovery_falls_back_to_flat_objects_for_non_db_sources(monkeypatch, fake_postgres) -> None:
    run = await discovery_service.create_run("org-1", "conn-1")

    async def _fake_discover(connection, timeout=8.0, *, org_id=None):
        return {"connected": True, "message": "ok", "schemas": [], "tables": [],
                "objects": [{"name": "users.csv", "type": "file"}]}

    monkeypatch.setattr(discovery_service_module, "discover", _fake_discover)

    await discovery_service.run_discovery("org-1", run["id"], CONNECTION)

    objects, _ = await discovery_service.list_objects("org-1", run["id"], search=None, limit=100, cursor=None)
    assert objects == [{"id": objects[0]["id"], "object_type": "file", "schema_name": None, "name": "users.csv"}]


@pytest.mark.asyncio
async def test_run_discovery_records_unreachable_source_without_failing_the_run(monkeypatch, fake_postgres) -> None:
    """connection_discovery.discover() never raises for an unreachable
    source — it returns connected=False with a message. That's a
    successful discovery attempt (it completed) that just found nothing;
    the reason is recorded on the run, not treated as a failure."""
    run = await discovery_service.create_run("org-1", "conn-1")

    async def _fake_discover(connection, timeout=8.0, *, org_id=None):
        return {"connected": False, "message": "Could not connect/introspect: timed out",
                "schemas": [], "tables": [], "objects": []}

    monkeypatch.setattr(discovery_service_module, "discover", _fake_discover)

    await discovery_service.run_discovery("org-1", run["id"], CONNECTION)

    updated = await discovery_service.get_run("org-1", run["id"])
    assert updated["status"] == "succeeded"
    assert updated["object_count"] == 0
    assert "timed out" in updated["error_message"]


@pytest.mark.asyncio
async def test_run_discovery_marks_failed_on_a_genuinely_unexpected_exception(monkeypatch, fake_postgres) -> None:
    run = await discovery_service.create_run("org-1", "conn-1")

    async def _boom(connection, timeout=8.0, *, org_id=None):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(discovery_service_module, "discover", _boom)

    with pytest.raises(RuntimeError):
        await discovery_service.run_discovery("org-1", run["id"], CONNECTION)

    updated = await discovery_service.get_run("org-1", run["id"])
    assert updated["status"] == "failed"
    assert "unexpected" in updated["error_message"]


@pytest.mark.asyncio
async def test_list_objects_search_filters_by_name_or_schema(monkeypatch, fake_postgres) -> None:
    run = await discovery_service.create_run("org-1", "conn-1")

    async def _fake_discover(connection, timeout=8.0, *, org_id=None):
        return {
            "connected": True, "message": "ok", "schemas": ["public"],
            "tables": [
                {"schema": "public", "name": "customers"},
                {"schema": "public", "name": "orders"},
            ],
            "objects": [],
        }

    monkeypatch.setattr(discovery_service_module, "discover", _fake_discover)
    await discovery_service.run_discovery("org-1", run["id"], CONNECTION)

    objects, _ = await discovery_service.list_objects("org-1", run["id"], search="custom", limit=100, cursor=None)
    assert [o["name"] for o in objects] == ["customers"]


@pytest.mark.asyncio
async def test_list_objects_paginates_with_cursor(monkeypatch, fake_postgres) -> None:
    run = await discovery_service.create_run("org-1", "conn-1")

    async def _fake_discover(connection, timeout=8.0, *, org_id=None):
        return {
            "connected": True, "message": "ok", "schemas": [],
            "tables": [{"schema": "public", "name": f"table_{i}"} for i in range(5)],
            "objects": [],
        }

    monkeypatch.setattr(discovery_service_module, "discover", _fake_discover)
    await discovery_service.run_discovery("org-1", run["id"], CONNECTION)

    page_1, cursor_1 = await discovery_service.list_objects("org-1", run["id"], search=None, limit=2, cursor=None)
    assert len(page_1) == 2
    assert cursor_1 is not None

    page_2, cursor_2 = await discovery_service.list_objects("org-1", run["id"], search=None, limit=2, cursor=cursor_1)
    assert len(page_2) == 2
    assert not ({o["id"] for o in page_1} & {o["id"] for o in page_2})  # no overlap

    page_3, cursor_3 = await discovery_service.list_objects("org-1", run["id"], search=None, limit=2, cursor=cursor_2)
    assert len(page_3) == 1
    assert cursor_3 is None  # exhausted


@pytest.mark.asyncio
async def test_list_objects_filters_by_object_type_and_schema_name(monkeypatch, fake_postgres) -> None:
    """FE-BUG-12: exact-match object_type/schema_name filters, so the UI can
    offer a Schema filter and an Object type filter instead of just search."""
    run = await discovery_service.create_run("org-1", "conn-1")

    async def _fake_discover(connection, timeout=8.0, *, org_id=None):
        return {
            "connected": True, "message": "ok", "schemas": ["public", "sales"],
            "tables": [
                {"schema": "public", "name": "customers"},
                {"schema": "public", "name": "orders"},
                {"schema": "sales", "name": "leads"},
            ],
            "objects": [],
        }

    monkeypatch.setattr(discovery_service_module, "discover", _fake_discover)
    await discovery_service.run_discovery("org-1", run["id"], CONNECTION)

    schemas_only, _ = await discovery_service.list_objects(
        "org-1", run["id"], search=None, limit=100, cursor=None, object_type="schema",
    )
    assert {o["name"] for o in schemas_only} == {"public", "sales"}

    sales_only, _ = await discovery_service.list_objects(
        "org-1", run["id"], search=None, limit=100, cursor=None, schema_name="sales",
    )
    assert {o["name"] for o in sales_only} == {"leads"}

    both, _ = await discovery_service.list_objects(
        "org-1", run["id"], search=None, limit=100, cursor=None, object_type="table", schema_name="public",
    )
    assert {o["name"] for o in both} == {"customers", "orders"}


@pytest.mark.asyncio
async def test_get_facets_returns_distinct_schema_names_and_object_types(monkeypatch, fake_postgres) -> None:
    run = await discovery_service.create_run("org-1", "conn-1")

    async def _fake_discover(connection, timeout=8.0, *, org_id=None):
        return {
            "connected": True, "message": "ok", "schemas": ["public", "sales"],
            "tables": [
                {"schema": "public", "name": "customers"},
                {"schema": "public", "name": "orders"},
                {"schema": "sales", "name": "leads"},
            ],
            "objects": [],
        }

    monkeypatch.setattr(discovery_service_module, "discover", _fake_discover)
    await discovery_service.run_discovery("org-1", run["id"], CONNECTION)

    facets = await discovery_service.get_facets("org-1", run["id"])
    assert facets["schema_names"] == ["public", "sales"]
    assert facets["object_types"] == ["schema", "table"]


@pytest.mark.asyncio
async def test_get_facets_is_org_scoped(fake_postgres) -> None:
    run = await discovery_service.create_run("org-1", "conn-1")
    with pytest.raises(HTTPException) as exc_info:
        await discovery_service.get_facets("org-2", run["id"])
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_list_objects_is_org_scoped(fake_postgres) -> None:
    run = await discovery_service.create_run("org-1", "conn-1")
    with pytest.raises(HTTPException) as exc_info:  # a different org can't even see the run exists
        await discovery_service.list_objects("org-2", run["id"], search=None, limit=100, cursor=None)
    assert exc_info.value.status_code == 404


# ── GAP-10: bounded size, readable order, idempotent re-execution, retention ───────────────────────────────────────


from datetime import UTC, datetime, timedelta  # noqa: E402

from app.core.config import settings  # noqa: E402


def _discover_returning(tables: list[dict], schemas: list[str] | None = None):
    async def _fake(connection, timeout=8.0, *, org_id=None):
        return {"connected": True, "message": "ok", "schemas": schemas or [], "tables": tables, "objects": []}

    return _fake


async def _discover_run(monkeypatch, tables, schemas=None):
    run = await discovery_service.create_run("org-1", "conn-1")
    monkeypatch.setattr(discovery_service_module, "discover", _discover_returning(tables, schemas))
    await discovery_service.run_discovery("org-1", run["id"], CONNECTION)
    return run


async def _all_names(run_id: str, *, limit: int) -> list[tuple[str | None, str]]:
    seen, cursor = [], None
    while True:
        page, cursor = await discovery_service.list_objects("org-1", run_id, search=None, limit=limit, cursor=cursor)
        seen += [(o["schema_name"], o["name"]) for o in page]
        if cursor is None:
            return seen


@pytest.mark.asyncio
async def test_objects_are_listed_by_schema_then_name_across_pages(monkeypatch, fake_postgres) -> None:
    tables = [{"schema": s, "name": n} for s, n in [
        ("sales", "orders"), ("hr", "staff"), ("sales", "customers"), ("hr", "assets"), ("sales", "returns"),
    ]]
    run = await _discover_run(monkeypatch, tables, schemas=["sales", "hr"])

    listed = await _all_names(run["id"], limit=2)

    assert listed == [
        (None, "hr"), (None, "sales"),  # schema rows first
        ("hr", "assets"), ("hr", "staff"),
        ("sales", "customers"), ("sales", "orders"), ("sales", "returns"),
    ]


@pytest.mark.asyncio
async def test_a_cursor_this_service_did_not_issue_is_a_400_not_a_500(monkeypatch, fake_postgres) -> None:
    run = await _discover_run(monkeypatch, [{"schema": "public", "name": "t"}])

    with pytest.raises(HTTPException) as exc_info:
        await discovery_service.list_objects("org-1", run["id"], search=None, limit=10, cursor="not-a-cursor!!")

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_a_run_over_the_cap_keeps_the_first_objects_and_is_marked_truncated(monkeypatch, fake_postgres) -> None:
    monkeypatch.setattr(settings, "discovery_max_objects", 5)
    tables = [{"schema": "public", "name": f"t{i}"} for i in range(8)]

    run = await _discover_run(monkeypatch, tables)

    finished = await discovery_service.get_run_or_404("org-1", run["id"])
    assert finished["truncated"] is True
    assert finished["object_count"] == 5
    assert len(await _all_names(run["id"], limit=100)) == 5  # what was stored is what the count says


@pytest.mark.asyncio
async def test_a_run_exactly_at_the_cap_is_not_truncated(monkeypatch, fake_postgres) -> None:
    monkeypatch.setattr(settings, "discovery_max_objects", 5)

    run = await _discover_run(monkeypatch, [{"schema": "public", "name": f"t{i}"} for i in range(5)])

    finished = await discovery_service.get_run_or_404("org-1", run["id"])
    assert finished["truncated"] is False
    assert finished["object_count"] == 5


@pytest.mark.asyncio
async def test_re_executing_a_run_replaces_its_objects_instead_of_duplicating_them(monkeypatch, fake_postgres) -> None:
    """The Operation driving a run can be claimed and executed again (its worker died, its lease lapsed)."""
    tables = [{"schema": "public", "name": f"t{i}"} for i in range(4)]
    run = await _discover_run(monkeypatch, tables)

    await discovery_service.run_discovery("org-1", run["id"], CONNECTION)  # second attempt

    stored = [r for r in fake_postgres.discovery_objects.values() if r.discovery_run_id == run["id"]]
    assert len(stored) == 4
    assert (await discovery_service.get_run_or_404("org-1", run["id"]))["object_count"] == 4


def _old_run(fake_postgres, *, connection_id: str, status: str, age_days: float) -> dict:
    run = fake_postgres.create("discovery_runs", {
        "org_id": "org-1", "connection_id": connection_id, "status": status,
        "created_at": datetime.now(UTC) - timedelta(days=age_days),
    })
    fake_postgres.create("discovery_objects", {
        "org_id": "org-1", "discovery_run_id": run["id"], "object_type": "table", "schema_name": "s", "name": "t",
    })
    return run


@pytest.mark.asyncio
async def test_pruning_removes_old_finished_runs_and_their_objects(fake_postgres) -> None:
    from app.services.discovery_service import prune_old_runs

    old_failed = _old_run(fake_postgres, connection_id="c1", status="failed", age_days=30)
    old_success = _old_run(fake_postgres, connection_id="c1", status="succeeded", age_days=20)
    latest_success = _old_run(fake_postgres, connection_id="c1", status="succeeded", age_days=10)
    recent = _old_run(fake_postgres, connection_id="c1", status="succeeded", age_days=0.5)

    removed = await prune_old_runs()

    survivors = set(fake_postgres.discovery_runs)
    assert removed == 3
    assert survivors == {recent["id"]}
    kept_objects = {o.discovery_run_id for o in fake_postgres.discovery_objects.values()}
    assert kept_objects == {recent["id"]}
    assert old_failed["id"] not in survivors
    assert old_success["id"] not in survivors
    assert latest_success["id"] not in survivors  # older than the retention AND superseded by a newer success


@pytest.mark.asyncio
async def test_pruning_always_keeps_a_connections_latest_successful_run_and_anything_in_flight(fake_postgres) -> None:
    from app.services.discovery_service import prune_old_runs

    only_success = _old_run(fake_postgres, connection_id="c1", status="succeeded", age_days=90)
    newer_failure = _old_run(fake_postgres, connection_id="c1", status="failed", age_days=60)
    still_running = _old_run(fake_postgres, connection_id="c2", status="running", age_days=90)
    queued = _old_run(fake_postgres, connection_id="c3", status="queued", age_days=90)

    removed = await prune_old_runs()

    assert removed == 1  # only the failed one
    assert set(fake_postgres.discovery_runs) == {only_success["id"], still_running["id"], queued["id"]}
    assert newer_failure["id"] not in fake_postgres.discovery_runs


@pytest.mark.asyncio
async def test_a_driver_asks_the_source_for_at_most_the_cap_plus_one_row(monkeypatch) -> None:
    """The cap is applied in the source's own query, so a huge catalogue is never pulled into memory first."""
    import types

    import app.services.connection_discovery as connection_discovery

    monkeypatch.setattr(settings, "discovery_max_objects", 7)
    queries: list[str] = []

    class _Conn:
        async def fetch(self, sql, *args):
            queries.append(sql)
            return []

        async def close(self):
            return None

    async def _connect(**kwargs):
        return _Conn()

    monkeypatch.setitem(__import__("sys").modules, "asyncpg", types.SimpleNamespace(connect=_connect))

    await connection_discovery._pg({"hostname": "h", "database": "d"}, 5.0)

    table_query = next(q for q in queries if "information_schema.tables" in q)
    assert table_query.rstrip().endswith("LIMIT 8")


@pytest.mark.asyncio
async def test_a_well_formed_cursor_with_a_forged_id_is_rejected_as_invalid(monkeypatch, fake_postgres) -> None:
    import base64
    import json

    run = await _discover_run(monkeypatch, [{"schema": "public", "name": "t"}])
    forged = base64.urlsafe_b64encode(json.dumps({"s": "", "n": "", "i": "not-a-uuid"}).encode()).decode()

    with pytest.raises(HTTPException) as exc_info:
        await discovery_service.list_objects("org-1", run["id"], search=None, limit=10, cursor=forged)

    assert exc_info.value.status_code == 400
