"""dbt / FlinkSQL / Python renderers: enforcement semantics (DQ-MODEL-002),
structured-plan compile (DQ-FLINK-008), contract sinks (DQ-CONTRACT-005),
aggregation + bounded samples (DQ-RESULT-010)."""
import pathlib
import re

import pytest
import yaml

from app.core import quality_contract as qc
from app.renderers.dbt import render_dbt
from app.renderers.flink import render_flink
from app.renderers.plan import PlanError, select_quality_source
from app.renderers.python import render_python
from tests.helpers import TOPICS, flink_ctx, item, plan, source_table

DBT_CTX = {"binding_id": "b1", "artifact_version_id": "a1"}


def _tests(text):
    model = yaml.safe_load(text)["models"][0]
    out = list(model.get("tests") or [])
    for c in model.get("columns") or []:
        out += c["tests"]
    return out


@pytest.mark.parametrize("mode,severity,tags", [
    ("blocking", "error", None), ("warn", "warn", None), ("async", "warn", ["dq_async"])])
def test_dbt_enforcement_mode_maps_exactly(mode, severity, tags):
    items = [item("not_null", "id", severity="INFO"), item("range", "amount", params={"min": 0},
                                                            severity="CRITICAL")]
    text, _ = render_dbt(items, target_table="analytics.orders", enforcement_mode=mode,
                         ctx={**DBT_CTX, "enforcement_mode": mode})
    for t in _tests(text):
        body = next(iter(t.values()))
        assert body["config"]["severity"] == severity
        assert body["config"].get("tags") == tags
        assert body["meta"]["dq_enforcement_mode"] == mode
    assert yaml.safe_load(text)["models"][0]["name"] == "orders"


def test_dbt_meta_traces_exact_versions():
    it = item("not_null", "id", rule_version=7)
    text, _ = render_dbt([it], target_table="orders", enforcement_mode="warn",
                         ctx={**DBT_CTX, "enforcement_mode": "warn"})
    meta = _tests(text)[0]["not_null"]["meta"]
    assert meta["dq_rule_version"] == 7 and meta["dq_check_id"] == it["item_id"]
    assert meta["dq_artifact_version_id"] == "a1" and meta["dq_binding_id"] == "b1"


def test_dbt_freshness_uses_recency_and_row_count_is_diagnosed():
    items = [item("freshness", "updated_at", params={"max_age_hours": 6}), item("row_count")]
    text, diags = render_dbt(items, target_table="orders", enforcement_mode="warn",
                             ctx={**DBT_CTX, "enforcement_mode": "warn"})
    t = _tests(text)[0]["dbt_utils.recency"]
    assert t["interval"] == 360 and t["field"] == '"updated_at"'
    assert diags[0]["rule_type"] == "row_count" and diags[0]["severity"] == "error"


def test_dbt_pg_lake_dialect_regex():
    text, _ = render_dbt([item("regex", "email", params={"pattern": "@"})], target_table="o",
                         enforcement_mode="warn", ctx={**DBT_CTX, "enforcement_mode": "warn"},
                         dialect="pg_lake")
    expr = _tests(text)[0]["dbt_utils.expression_is_true"]["expression"]
    assert "CAST(\"email\" AS TEXT) ~ '@'" in expr


# ── Flink ─────────────────────────────────────────────────────────────

def _flink(items, mode="warn", **kw):
    return render_flink(items, source=source_table(), ctx=flink_ctx(mode, **kw), topics=TOPICS)


def test_flink_source_is_built_from_the_plan_with_own_consumer_group():
    ctx = flink_ctx()
    sql, _ = render_flink([item("not_null", "order_id")], source=source_table(), ctx=ctx,
                          topics=TOPICS)
    src = sql.split("CREATE TABLE `dq_results_sink`")[0]
    for col, typ in (("order_id", "BIGINT"), ("amount", "DECIMAL(10, 2)"),
                     ("updated_at", "TIMESTAMP(3)")):
        assert f"`{col}` {typ}" in src
    assert "`__dq_proc_time` AS PROCTIME()" in src
    assert f"'properties.group.id' = 'dq-quality-{ctx['binding_id']}'" in src
    assert "transform-group" not in src and "earliest-offset" not in src
    assert "'scan.startup.mode' = 'group-offsets'" in src
    assert "'json.ignore-parse-errors' = 'true'" in src


def test_flink_sinks_come_from_the_shared_contract():
    sql, _ = _flink([item("not_null", "order_id")])
    sink = sql.split("CREATE TABLE `dq_results_sink`")[1].split(";")[0]
    for f in qc.fields("window_result"):
        assert f"`{f['name']}` {f['flink_type']}" in sink
    assert f"'topic' = '{qc.RESULTS_TOPIC}'" in sink
    assert "`dq_gate_sink`" not in sql and "`dq_quarantine_sink`" not in sql


def test_flink_window_result_columns_in_contract_order_and_aggregated():
    sql, _ = _flink([item("range", "amount", params={"min": 0, "threshold": 0.01})])
    view = sql.split("CREATE TEMPORARY VIEW `dq_check_1` AS")[1].split(";")[0]
    names = re.findall(r" AS `(\w+)`", view)
    assert names == qc.field_names("window_result")
    assert "TUMBLE(TABLE `dq_source`, DESCRIPTOR(`__dq_proc_time`), INTERVAL '1' MINUTE)" in view
    assert "GROUP BY window_start, window_end" in view
    assert "CAST(0.01 AS DOUBLE) AS `threshold`" in view
    assert "'${DQ_RUN_REF}' AS `run_ref`" in view


def test_flink_samples_are_bounded_and_1_19_compatible():
    sql, _ = _flink([item("regex", "email", params={"pattern": "@"})])
    assert "ARRAY_AGG" not in sql
    assert sql.count("FIRST_VALUE(") == 1 and sql.count("MAX(CASE") == 1
    assert f"FOR {qc.MAX_SAMPLE_CHARS})" in sql
    sql0, _ = render_flink([item("regex", "email", params={"pattern": "@"})],
                           source=source_table(), ctx=flink_ctx(), topics=TOPICS, sample_slots=0)
    assert "CAST(NULL AS ARRAY<STRING>) AS `sample_values`" in sql0


def test_flink_blocking_renders_quarantine_and_gate_without_fail_stop_by_default():
    sql, diags = _flink([item("not_null", "order_id")], mode="blocking")
    assert f"'topic' = '{qc.GATE_TOPIC}'" in sql and f"'topic' = '{qc.QUARANTINE_TOPIC}'" in sql
    assert "'quarantine' AS `action`" in sql and "fail_stop" not in sql.split("BEGIN")[1]
    assert "JSON_OBJECT(KEY 'order_id' VALUE `order_id`" in sql
    assert "CAST(`violation_count` AS DOUBLE) > `threshold`" in sql


def test_flink_fail_stop_only_when_opted_in():
    sql, _ = _flink([item("not_null", "order_id")], mode="blocking", fail_stop=True)
    assert "'fail_stop' AS `action`" in sql


def test_flink_async_equals_warn_apart_from_mode_label():
    a, _ = _flink([item("not_null", "order_id")], mode="async")
    w, _ = _flink([item("not_null", "order_id")], mode="warn")
    assert "dq_gate_sink" not in a and a.count("INSERT INTO") == w.count("INSERT INTO") == 1


def test_flink_column_existence_and_type_checked_against_plan():
    items = [item("not_null", "missing_col"), item("range", "status", params={"min": 1}),
             item("freshness", "amount", params={"max_age_hours": 1}), item("unique", "order_id"),
             item("not_null", "order_id")]
    sql, diags = _flink(items)
    msgs = " | ".join(d["message"] for d in diags)
    assert "does not exist in source table" in msgs and "numeric column" in msgs
    assert "TIMESTAMP/DATE column" in msgs and "dedup job" in msgs
    assert sql.count("CREATE TEMPORARY VIEW") == 1


def test_flink_nothing_renderable_yields_no_artifact():
    sql, diags = _flink([item("unique", "order_id")])
    assert sql is None and diags


def test_select_quality_source_rules():
    a = source_table("a")
    b = source_table("b")
    out_sink = source_table("orders_out")
    p = plan(sources=[a, b], sinks=[out_sink])
    assert select_quality_source(p, "orders_out")[0].name == "orders_out"
    assert select_quality_source(p, "catalog.db.b")[0].name == "b"
    with pytest.raises(PlanError, match="2 Kafka sources"):
        select_quality_source(p, "nope")
    single = plan(sources=[a])
    t, diags = select_quality_source(single, "nope")
    assert t.name == "a" and diags
    jdbc = source_table("j", options={"connector": "jdbc"})
    with pytest.raises(PlanError, match="connector"):
        select_quality_source(plan(sources=[jdbc]), "j")
    with pytest.raises(PlanError, match="no Kafka source"):
        select_quality_source(plan(sources=[jdbc]), "x")


def test_no_textual_sql_parsing_anywhere_in_dq():
    """DQ-FLINK-008: DQ never regex-parses SQL text (parent or otherwise) to
    extract DDL — only the runner's structured plan is used."""
    root = pathlib.Path(__file__).resolve().parent.parent / "app"
    pattern = re.compile(r"CREATE\\?s?\+?\s*TABLE|INSERT\\?s?\+?\s*INTO", re.IGNORECASE)
    for f in root.rglob("*.py"):
        for line in f.read_text().splitlines():
            if "re.compile" in line or "re.findall" in line or "re.search" in line \
                    or "re.match" in line:
                assert not pattern.search(line), f"{f}: {line}"


# ── Python ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("mode,on_failure", [("blocking", "block"), ("warn", "warn"),
                                             ("async", "async")])
def test_python_artifacts_carry_outcome_contract(mode, on_failure):
    gx, soda, _ = render_python([item("not_null", "id", params={"threshold": 0.1})],
                                target_table="orders", enforcement_mode=mode,
                                ctx={"binding_id": "b", "artifact_version_id": "a"})
    assert gx["meta"]["on_failure"] == on_failure
    assert gx["expectations"][0]["kwargs"]["mostly"] == 0.9
    assert f"# on_failure: {on_failure}" in soda
    assert "missing_percent(id) <= 10.0" in soda
