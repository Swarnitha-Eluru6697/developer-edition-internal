"""Data dependencies between projects, worked out from dbt's own lineage (for Dagster's asset lineage).

Project B reads project A's data when a table B's latest successful build read is one A's latest successful build wrote. Nothing is
configured: the runner already records every successful run's lineage. Exposed as `data_upstream_project_ids` on the project, including
the internal listing Dagster builds its code locations from.

Real migrated Postgres; projects, runs and lineage rows are written directly.
"""
from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from jose import jwt

from app.core.config import get_settings
from app.main import app
from app.models import LineageEdge, Project, Run
from app.services.data_dependencies import data_upstream, table_refs
from app.services.run_service import _extract_lineage

SECRET = "dagster-scheduler-signing-secret"
T = '"dbtrunner"."public".'


@pytest.fixture(autouse=True)
def scheduler_identity(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "scheduler_jwt_secret", SECRET)
    monkeypatch.setattr(s, "scheduler_jwt_allowed_issuers", ["dagster"])


@pytest.fixture
async def api(run_db):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _token() -> str:
    now = int(time.time())
    return jwt.encode({"iss": "dagster", "sub": "dagster", "aud": "dbtrunner", "scope": "dbt.projects.read",
                       "org_id": get_settings().org_id, "jti": uuid.uuid4().hex, "iat": now, "exp": now + 60},
                      SECRET, algorithm="HS256")


async def _project(run_db, name, *, org_id=None) -> uuid.UUID:
    async with run_db() as db:
        p = Project(org_id=org_id or get_settings().org_id, name=f"inaiera-dbt-test/{name}",
                    git_url=f"https://github.com/acme/{name}-{uuid.uuid4().hex[:6]}.git", git_ref="main", engine="pg_lake")
        db.add(p)
        await db.commit()
        return p.id


async def _build(run_db, project_id, edges, *, status="succeeded", minutes_ago=0) -> None:
    """A finished run of `project_id` whose lineage is `edges` [(source, target), ...]."""
    at = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    async with run_db() as db:
        run = Run(project_id=project_id, trigger="manual", status=status, attempt=1, started_at=at, completed_at=at)
        db.add(run)
        await db.flush()
        for src, tgt in edges:
            db.add(LineageEdge(run_id=run.id, project_id=project_id, source_table=src, target_table=tgt,
                               relationship_type="source" if "raw" in src or "dim_" in src or "fct_" in src else "ref"))
        await db.commit()


async def _upstream(run_db):
    async with run_db() as db:
        return await data_upstream(db, get_settings().org_id)


async def test_a_project_reading_a_table_another_project_builds_depends_on_it(run_db):
    customers, products = await _project(run_db, "customers"), await _project(run_db, "products")
    await _build(run_db, customers, [(f"{T}\"raw_customers\"", f"{T}\"dim_customers\"")])
    await _build(run_db, products, [(f"{T}\"dim_customers\"", f"{T}\"product_orders\"")])
    assert await _upstream(run_db) == {products: [customers]}


async def test_quoting_and_case_do_not_hide_a_link(run_db):
    a, b = await _project(run_db, "customers"), await _project(run_db, "products")
    await _build(run_db, a, [("raw.x", '"DBTRUNNER"."PUBLIC"."DIM_CUSTOMERS"')])
    await _build(run_db, b, [("dbtrunner.public.dim_customers", "dbtrunner.public.product_orders")])
    assert await _upstream(run_db) == {b: [a]}


async def test_a_chain_and_a_fan_in_are_both_seen(run_db):
    customers, products, revenue, combined = [await _project(run_db, n) for n in ("customers", "products", "revenue", "combined")]
    await _build(run_db, customers, [("raw.c", "dim_customers")])
    await _build(run_db, products, [("dim_customers", "fct_products")])
    await _build(run_db, revenue, [("dim_customers", "fct_revenue")])
    await _build(run_db, combined, [("fct_products", "combined_x"), ("fct_revenue", "combined_x")])
    assert await _upstream(run_db) == {products: [customers], revenue: [customers], combined: [products, revenue]}


async def test_only_the_latest_successful_build_counts(run_db):
    a, b = await _project(run_db, "customers"), await _project(run_db, "products")
    await _build(run_db, a, [("raw.c", "dim_customers")], minutes_ago=60)
    await _build(run_db, b, [("dim_customers", "fct_products")], minutes_ago=60)
    await _build(run_db, b, [("raw.p", "fct_products")])                    # products stopped reading customers' table
    assert await _upstream(run_db) == {}


async def test_a_failed_run_does_not_change_the_links(run_db):
    a, b = await _project(run_db, "customers"), await _project(run_db, "products")
    await _build(run_db, a, [("raw.c", "dim_customers")], minutes_ago=60)
    await _build(run_db, b, [("dim_customers", "fct_products")], minutes_ago=60)
    await _build(run_db, b, [("raw.p", "fct_products")], status="failed")
    assert await _upstream(run_db) == {b: [a]}


async def test_a_table_a_project_builds_itself_is_never_another_projects_data(run_db):
    """Both projects build a table with the same name in the same schema: neither reads the other's."""
    a, b = await _project(run_db, "customers"), await _project(run_db, "products")
    await _build(run_db, a, [("stg_orders", "dim_customers"), ("raw.o", "stg_orders")])
    await _build(run_db, b, [("stg_orders", "fct_products"), ("raw.o", "stg_orders")])
    assert await _upstream(run_db) == {}


async def test_another_orgs_projects_are_never_linked(run_db):
    ours, theirs = await _project(run_db, "products"), await _project(run_db, "customers", org_id="another-org")
    await _build(run_db, theirs, [("raw.c", "dim_customers")])
    await _build(run_db, ours, [("dim_customers", "fct_products")])
    assert await _upstream(run_db) == {}


async def test_dagster_and_the_ui_get_the_links_on_the_project(api, auth_headers, run_db):
    customers, products = await _project(run_db, "customers"), await _project(run_db, "products")
    await _build(run_db, customers, [("raw.c", "dim_customers")])
    await _build(run_db, products, [("dim_customers", "fct_products")])

    internal = (await api.get("/api/v1/internal/projects", headers={"X-Internal-Service-Token": _token()})).json()
    by_name = {p["name"]: p for p in internal}
    assert by_name["inaiera-dbt-test/products"]["data_upstream_project_ids"] == [str(customers)]
    assert by_name["inaiera-dbt-test/customers"]["data_upstream_project_ids"] == []

    public = (await api.get(f"/api/v1/projects/{products}", headers=auth_headers)).json()
    assert public["data_upstream_project_ids"] == [str(customers)]


# ── tables named directly in SQL (no dbt source) ─────────────────────────────────────────────────────────────────────────

EXEC_SQL = """
-- old version: select * from public.legacy_summary
/* also once: join public.retired_table */
with c as (select * from {{ ref('exec_upstream_check') }})
select c.products_fresh,
       (select city from public.city_revenue order by revenue desc limit 1) as top_city,
       'copied from public.not_a_table' as note
from "dbtrunner"."public"."exec_upstream_check" c
cross join (select sum(revenue) as total_revenue from public.monthly_revenue) r
left join Public.Product_Performance p on true
join stg_local s on true
"""


def test_table_refs_reads_only_qualified_names_after_from_or_join():
    assert table_refs(EXEC_SQL) == [
        "public.city_revenue", "dbtrunner.public.exec_upstream_check", "public.monthly_revenue", "public.product_performance"]


def _node(name, sql, deps=(), rtype="model"):
    return {"resource_type": rtype, "name": name, "relation_name": f'"dbtrunner"."public"."{name}"',
            "compiled_code": sql, "depends_on": {"nodes": list(deps)}}


def test_lineage_adds_sql_edges_only_for_tables_this_project_does_not_build_or_already_reference():
    manifest = {"nodes": {
        "model.e.exec_upstream_check": _node("exec_upstream_check", "select 1"),
        "model.e.exec_summary": _node("exec_summary", EXEC_SQL, deps=["model.e.exec_upstream_check"]),
    }, "sources": {}}
    edges = _extract_lineage(manifest)
    assert ('"dbtrunner"."public"."exec_upstream_check"', '"dbtrunner"."public"."exec_summary"', "ref") in edges
    sql = sorted(src for src, _, rel in edges if rel == "sql")
    assert sql == ["public.city_revenue", "public.monthly_revenue", "public.product_performance"]   # not its own exec_upstream_check


async def test_the_owners_repo_gets_its_full_chain(run_db):
    """customers -> products/revenue (dbt sources) -> combined/executive (direct SQL) -> scorecard (direct SQL); inventory alone."""
    ids = {n: await _project(run_db, n) for n in ("customers", "products", "revenue", "combined", "executive", "scorecard", "inventory")}
    q = '"dbtrunner"."public".'
    await _build(run_db, ids["customers"], [("raw.c", f"{q}\"dim_customers\"")])
    await _build(run_db, ids["products"], [(f"{q}\"dim_customers\"", f"{q}\"product_performance\"")])
    await _build(run_db, ids["revenue"], [(f"{q}\"dim_customers\"", f"{q}\"monthly_revenue\"")])
    await _build(run_db, ids["combined"], [("public.product_performance", f"{q}\"revenue_reconciliation\""),
                                           ("public.monthly_revenue", f"{q}\"revenue_reconciliation\"")])
    await _build(run_db, ids["executive"], [("public.monthly_revenue", f"{q}\"exec_summary\""),
                                            ("public.product_performance", f"{q}\"exec_summary\"")])
    await _build(run_db, ids["scorecard"], [("public.revenue_reconciliation", f"{q}\"business_scorecard\"")])
    await _build(run_db, ids["inventory"], [("raw.i", f"{q}\"stock\"")])
    up = await _upstream(run_db)
    assert up == {ids["products"]: [ids["customers"]], ids["revenue"]: [ids["customers"]],
                  ids["combined"]: [ids["products"], ids["revenue"]], ids["executive"]: [ids["products"], ids["revenue"]],
                  ids["scorecard"]: [ids["combined"]]}


async def test_a_table_built_by_two_other_projects_is_not_guessed(run_db):
    a, b, reader = [await _project(run_db, n) for n in ("customers", "products", "combined")]
    await _build(run_db, a, [("raw.x", '"dbtrunner"."public"."shared"')])
    await _build(run_db, b, [("raw.y", '"dbtrunner"."public"."shared"')])
    await _build(run_db, reader, [("public.shared", '"dbtrunner"."public"."combined_x"')])
    assert await _upstream(run_db) == {}
