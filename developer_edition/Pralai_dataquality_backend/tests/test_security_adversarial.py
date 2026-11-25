"""DQ-SEC-006: engine-specific quoting, typed literals, custom_sql lexer,
compiled-artifact validation — adversarial inputs."""
import json

import pytest
import yaml

from app.renderers.dbt import render_dbt
from app.renderers.flink import render_flink, flink_sinks
from app.renderers.python import render_python
from app.renderers.security import (
    RenderSecurityError, UnsafeCustomSqlError, quote_flink_identifier, quote_sql_identifier,
    render_sql_literal, sql_string, validate_custom_sql_condition, validate_fqn,
)
from app.renderers.validation import (
    validate_dbt_artifact, validate_flink_artifact, validate_python_artifacts,
)
from tests.helpers import TOPICS, flink_ctx, item, source_table

DBT_CTX = {"binding_id": "b1", "artifact_version_id": "a1", "enforcement_mode": "warn"}


# ── primitives ─────────────────────────────────────────────────────────

def test_sql_identifier_doubles_embedded_quotes():
    assert quote_sql_identifier('a"b') == '"a""b"'
    assert quote_sql_identifier("order") == '"order"'


def test_flink_identifier_doubles_embedded_backticks():
    assert quote_flink_identifier("a`b") == "`a``b`"


@pytest.mark.parametrize("bad", ["", "a\nb", "a\x00b", "x" * 300, None, 5])
def test_identifiers_with_control_chars_or_bad_types_rejected(bad):
    with pytest.raises(RenderSecurityError):
        quote_sql_identifier(bad)


def test_literals_are_typed():
    assert render_sql_literal(True) == "TRUE"
    assert render_sql_literal(3) == "3" and render_sql_literal(2.5) == "2.5"
    assert render_sql_literal("it's") == "'it''s'"
    with pytest.raises(RenderSecurityError):
        render_sql_literal(float("nan"))
    with pytest.raises(RenderSecurityError):
        render_sql_literal({"a": 1})
    with pytest.raises(RenderSecurityError):
        sql_string("line1\nline2")


@pytest.mark.parametrize("fqn,ok", [("pg.public.orders", True), ("iceberg.silver.orders", True),
                                    ("a.b'c", False), ("one", False), ("a.b.c.d.e", False),
                                    ("a..b", False), ("a.b;drop", False)])
def test_fqn_validation(fqn, ok):
    if ok:
        assert validate_fqn(fqn) == fqn
    else:
        with pytest.raises(RenderSecurityError):
            validate_fqn(fqn)


@pytest.mark.parametrize("cond", [
    "amount > 0; DROP TABLE x", "amount > 0 -- c", "amount /* x */ > 0",
    "amount > (SELECT max(a) FROM secrets)", "1=1 UNION SELECT 1", "x IN (VALUES (1))",
    "amount > 0)", "(amount > 0", "name = 'unterminated", "a\\'b", "CALL proc()",
    "amount > 0 AND set_config('a','b')=1 OR 1=1; SET x=1", "delete_flag = 1 or DELETE",
])
def test_custom_sql_lexer_rejects_injection(cond):
    with pytest.raises(UnsafeCustomSqlError):
        validate_custom_sql_condition(cond)


@pytest.mark.parametrize("cond", [
    "amount > 0", "status IN ('a', 'b')", "name = 'O''Brien; DROP'", '"select" > 1',
    "coalesce(updated_flag, 0) = 1", "from_date <= to_date",
])
def test_custom_sql_lexer_accepts_real_predicates(cond):
    assert validate_custom_sql_condition(cond) == cond


# ── dbt ───────────────────────────────────────────────────────────────

def test_dbt_hostile_column_and_pattern_stay_inside_quoting():
    col = 'evil" ) OR 1=1 OR ("'
    items = [item("regex", col, params={"pattern": "^a'b$"}),
             item("not_null", col), item("accepted_values", col, params={"values": ["x'y", 1]})]
    text, diags = render_dbt(items, target_table="orders", enforcement_mode="warn", ctx=DBT_CTX)
    assert not [d for d in diags if d["severity"] == "error"], diags
    doc = yaml.safe_load(text)
    model = doc["models"][0]
    expr = model["tests"][0]["dbt_utils.expression_is_true"]["expression"]
    assert '"evil"" ) OR 1=1 OR ("""' in expr and "'^a''b$'" in expr
    col_entry = model["columns"][0]
    assert col_entry["name"] == col and col_entry["quote"] is True
    assert validate_dbt_artifact(text, model_name="orders", enforcement_mode="warn") == []


def test_dbt_yaml_cannot_be_broken_by_rule_names():
    items = [item("not_null", "id", name="x\n  - malicious: true\n'\"")]
    text, _ = render_dbt(items, target_table="orders", enforcement_mode="blocking", ctx=DBT_CTX)
    doc = yaml.safe_load(text)
    assert list(doc) == ["version", "models"] and len(doc["models"]) == 1
    assert validate_dbt_artifact(text, model_name="orders", enforcement_mode="blocking") == []


def test_dbt_validator_rejects_tampered_artifacts():
    good, _ = render_dbt([item("not_null", "id")], target_table="orders", enforcement_mode="warn",
                         ctx=DBT_CTX)
    doc = yaml.safe_load(good)
    doc["models"][0]["columns"][0]["tests"].append({"dbt_utils.run_query": {"sql": "drop"}})
    errs = validate_dbt_artifact(yaml.safe_dump(doc), model_name="orders", enforcement_mode="warn")
    assert any("not an allowed DQ test" in e for e in errs)
    assert validate_dbt_artifact(good, model_name="other", enforcement_mode="warn")
    assert validate_dbt_artifact(good, model_name="orders", enforcement_mode="blocking")


def test_dbt_unapproved_custom_sql_never_renders():
    items = [item("custom_sql", params={"condition": "amount > 0"}, review_status="pending")]
    text, diags = render_dbt(items, target_table="orders", enforcement_mode="warn", ctx=DBT_CTX)
    assert text is None and "not been approved" in diags[0]["message"]


def test_dbt_custom_sql_injection_rejected_at_compile_even_if_stored():
    items = [item("custom_sql", params={"condition": "1=1; DROP TABLE t"})]
    text, diags = render_dbt(items, target_table="orders", enforcement_mode="warn", ctx=DBT_CTX)
    assert text is None and "invalid parameters" in diags[0]["message"]


# ── Flink ─────────────────────────────────────────────────────────────

def test_flink_hostile_values_quoted_and_artifact_validates():
    src = source_table(columns=[("we`ird", "STRING"), ("amount", "DOUBLE")],
                       options={"connector": "kafka", "topic": "t'; DROP TABLE x; --",
                                "format": "json"})
    items = [item("accepted_values", "we`ird", params={"values": ["a'); DROP TABLE x; --"]}),
             item("regex", "we`ird", params={"pattern": "^x'--$"}),
             item("range", "amount", params={"min": 0}, name="r'); DROP TABLE x; --"),
             item("not_null", "amount", name="multi\nline")]
    sql, diags = render_flink(items, source=src, ctx=flink_ctx(), topics=TOPICS)
    errors = [d for d in diags if d["severity"] == "error"]
    assert sql and len(errors) == 1 and "control characters" in errors[0]["message"]
    assert "'r''); DROP TABLE x; --'" in sql
    assert "`we``ird`" in sql and "'a''); DROP TABLE x; --'" in sql
    assert "'t''; DROP TABLE x; --'" in sql
    assert validate_flink_artifact(sql, sinks=flink_sinks("warn"), topics=TOPICS) == []


def test_flink_unsafe_option_key_from_plan_rejected():
    src = source_table(options={"connector": "kafka", "topic": "t", "bad' = 'x": "y"})
    with pytest.raises(RenderSecurityError):
        render_flink([item("not_null", "order_id")], source=src, ctx=flink_ctx(), topics=TOPICS)


def test_flink_unsafe_type_from_plan_rejected():
    src = source_table(columns=[("a", "STRING); DROP TABLE x; --")])
    with pytest.raises(RenderSecurityError):
        render_flink([item("not_null", "a")], source=src, ctx=flink_ctx(), topics=TOPICS)


@pytest.mark.parametrize("tamper,expect", [
    (lambda s: s + "\nDROP TABLE `dq_source`;", "DROP"),
    (lambda s: s.replace("INSERT INTO `dq_results_sink`", "INSERT INTO `customer_table`"),
     "non-contract table"),
    (lambda s: s.replace("dq.quality.results.v2", "attacker.topic"), "contract requires"),
    (lambda s: "SET 'pipeline.name' = 'x';\n" + s, "SET"),
    (lambda s: s + "\nCREATE TABLE `other` (a INT) WITH ('connector'='kafka');", "not an allowed"),
])
def test_flink_validator_rejects_tampered_artifacts(tamper, expect):
    sql, _ = render_flink([item("not_null", "order_id")], source=source_table(), ctx=flink_ctx(),
                          topics=TOPICS)
    errs = validate_flink_artifact(tamper(sql), sinks=flink_sinks("warn"), topics=TOPICS)
    assert any(expect in e for e in errs), errs


# ── Python ────────────────────────────────────────────────────────────

def test_soda_yaml_values_are_serialized_not_concatenated():
    items = [item("regex", "email", params={"pattern": "a\": x - injected"}, name="n: v - x: [y]"),
             item("accepted_values", "status", params={"values": ["a]", "b, c"]})]
    gx, soda, diags = render_python(items, target_table="orders", enforcement_mode="warn",
                                    ctx={"binding_id": "b", "artifact_version_id": "a"})
    doc = yaml.safe_load(soda)
    checks = doc["checks for orders"]
    assert len(checks) == 2
    assert validate_python_artifacts(gx, soda) == []
    json.dumps(gx)


def test_soda_rejects_column_with_double_quote():
    gx, soda, diags = render_python([item("not_null", 'a"b')], target_table="orders",
                                    enforcement_mode="warn",
                                    ctx={"binding_id": "b", "artifact_version_id": "a"})
    assert any("injection guard" in d["message"] for d in diags)
