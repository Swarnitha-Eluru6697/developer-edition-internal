"""Applying Data Quality tests must not break a project that uses dbt's `data_tests:` key.

dbt 1.8 renamed a model's/column's `tests:` to `data_tests:` and refuses an entry that has both ("Invalid models config"). DQ
renders `tests:`; merging them into a column that already declared `data_tests:` produced both keys, so once a ruleset was applied
to such a project EVERY build failed at parse time — and with an empty error message, because a parse error leaves dbt no node
results to report. Now DQ's tests go under the key the entry already uses, and a failed run always says why.

And a DQ check used to be DROPPED when the project already had a test of the same kind on that column (dbt would have named both
alike); so on the owner's customers project, whose own tests cover customer_id and customer_segment, no DQ check ran at all. Every DQ
test now carries its own dbt test name and runs next to the project's.

The merge is checked against real dbt (`dbt parse`, no database needed).
"""
from __future__ import annotations

import types
from pathlib import Path

import yaml

from app.executors.dbt_executor import _extract_errors
from app.services.run_service import _apply_dq_schema_tests


def _dq(name, extra=None, meta_id="c1"):
    body = {"config": {"severity": "warn"}, "meta": {"dq_rule": name, "dq_check_id": meta_id}}
    body.update(extra or {})
    return {name: body}


DQ_BLOCK = {"dim_customers": yaml.dump({"version": 2, "models": [{
    "name": "dim_customers",
    "tests": [_dq("dbt_utils.expression_is_true", {"expression": "1 = 1"}, "m1")],
    "columns": [
        {"name": "customer_id", "tests": [_dq("not_null"), _dq("unique")]},
        {"name": "customer_segment", "tests": [_dq("accepted_values", {"values": ["high", "medium", "low"]}, "c3")]},
    ]}]})}

# The owner's customers project, as it is: its own tests on customer_id under `data_tests:`.
PROJECT_YAML = """version: 2
models:
  - name: dim_customers
    description: One row per customer.
    data_tests:
      - dbt_utils.expression_is_true:
          expression: "customer_id is not null"
    columns:
      - name: customer_id
        data_tests: [unique, not_null]
      - name: customer_segment
"""


def _project(tmp_path: Path, schema_yaml: str = PROJECT_YAML) -> Path:
    root = tmp_path / "customers"
    (root / "models" / "marts").mkdir(parents=True)
    (root / "dbt_project.yml").write_text("name: customers\nversion: '1.0'\nconfig-version: 2\nprofile: inaiera\n")
    (root / "models" / "marts" / "dim_customers.sql").write_text("select 1 as customer_id, 'high' as customer_segment\n")
    (root / "models" / "marts" / "_marts__models.yml").write_text(schema_yaml)
    return root


def _model(root: Path) -> dict:
    return yaml.safe_load((root / "models" / "marts" / "_marts__models.yml").read_text())["models"][0]


def _names(tests):
    return [next(iter(t)) if isinstance(t, dict) else t for t in tests]


def _test_names(tests):
    return [next(iter(t.values())).get("name") if isinstance(t, dict) else t for t in tests]


def test_dq_tests_go_under_data_tests_when_the_column_uses_data_tests(tmp_path):
    root = _project(tmp_path)
    _apply_dq_schema_tests(DQ_BLOCK, root)
    model = _model(root)
    cols = {c["name"]: c for c in model["columns"]}
    assert "tests" not in cols["customer_id"] and "tests" not in model          # never both keys on one entry
    # the project's own tests stay first and unchanged; DQ's run next to them under their own names
    assert _test_names(cols["customer_id"]["data_tests"]) == [
        "unique", "not_null", "dq_not_null_dim_customers_customer_id_c1", "dq_unique_dim_customers_customer_id_c1"]
    assert _test_names(cols["customer_segment"]["tests"]) == ["dq_accepted_values_dim_customers_customer_segment_c3"]
    assert _test_names(model["data_tests"]) == [None, "dq_expression_is_true_dim_customers_m1"]   # model level too


def test_applying_twice_does_not_stack_copies(tmp_path):
    root = _project(tmp_path)
    _apply_dq_schema_tests(DQ_BLOCK, root)
    first = _model(root)
    _apply_dq_schema_tests(DQ_BLOCK, root)
    assert _model(root) == first


def test_a_project_using_the_old_tests_key_is_unchanged(tmp_path):
    root = _project(tmp_path, PROJECT_YAML.replace("data_tests", "tests"))
    _apply_dq_schema_tests(DQ_BLOCK, root)
    cols = {c["name"]: c for c in _model(root)["columns"]}
    assert "data_tests" not in cols["customer_id"]
    assert _names(cols["customer_id"]["tests"]) == ["unique", "not_null", "not_null", "unique"]


def test_the_merged_project_parses_in_real_dbt_with_dq_tests_next_to_the_projects_own(tmp_path):
    """The live failures: `Parsing Error  Invalid models config given in models/marts/_marts__models.yml`, and then (once that was
    fixed) DQ's checks not running at all because the project already had tests of the same kind."""
    from dbt.cli.main import dbtRunner

    root = _project(tmp_path, PROJECT_YAML.replace(
        '    data_tests:\n      - dbt_utils.expression_is_true:\n          expression: "customer_id is not null"\n', "").replace(
        "      - name: customer_segment\n",
        "      - name: customer_segment\n        data_tests:\n          - accepted_values:\n"
        "              values: [high, medium, low, no_purchases]\n"))
    _apply_dq_schema_tests({"dim_customers": yaml.dump({"version": 2, "models": [{
        "name": "dim_customers",
        "columns": [{"name": "customer_id", "tests": [_dq("not_null"), _dq("unique", meta_id="c2")]},
                    {"name": "customer_segment", "tests": [_dq("accepted_values", {"values": ["high"]}, "c3")]}]}]})}, root)
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "profiles.yml").write_text(
        "inaiera:\n  target: t\n  outputs:\n    t:\n      type: postgres\n      host: localhost\n      port: 5432\n"
        "      user: x\n      password: x\n      dbname: x\n      schema: public\n      threads: 1\n")
    result = dbtRunner().invoke(["parse", "--project-dir", str(root), "--profiles-dir", str(profiles), "--no-partial-parse"])
    assert result.success, result.exception
    tests = sorted(n.name for n in result.result.nodes.values() if n.resource_type == "test")
    assert tests == sorted([
        "accepted_values_dim_customers_customer_segment__high__medium__low__no_purchases",   # the project's own
        "not_null_dim_customers_customer_id", "unique_dim_customers_customer_id",              # the project's own
        "dq_accepted_values_dim_customers_customer_segment_c3",                                # DQ's, all present
        "dq_not_null_dim_customers_customer_id_c1", "dq_unique_dim_customers_customer_id_c2"])


def test_a_failure_before_any_node_ran_still_has_a_reason():
    parse_error = types.SimpleNamespace(success=False, result=None, exception=Exception("Parsing Error: Invalid models config"))
    assert _extract_errors(parse_error) == ["Parsing Error: Invalid models config"]
    silent = types.SimpleNamespace(success=False, result=None, exception=None)
    assert _extract_errors(silent) == ["dbt failed without reporting a reason (see the run's logs)"]
    node_failure = types.SimpleNamespace(success=False, exception=None, result=types.SimpleNamespace(
        results=[types.SimpleNamespace(status="error", message="relation does not exist")]))
    assert _extract_errors(node_failure) == ["relation does not exist"]


def test_an_updated_ruleset_replaces_the_previous_copy_of_its_tests(tmp_path):
    root = _project(tmp_path)
    _apply_dq_schema_tests(DQ_BLOCK, root)
    doc = yaml.safe_load(DQ_BLOCK["dim_customers"])
    doc["models"][0]["columns"][1]["tests"][0]["accepted_values"]["values"].append("no_purchases")
    changed = {"dim_customers": yaml.dump(doc)}
    _apply_dq_schema_tests(changed, root)
    seg = next(c for c in _model(root)["columns"] if c["name"] == "customer_segment")
    (only,) = seg["tests"]
    assert only["accepted_values"]["values"] == ["high", "medium", "low", "no_purchases"]


def test_a_project_test_that_already_has_a_dq_tests_name_is_not_duplicated(tmp_path):
    """Two tests with one name are a dbt error; a project that copied a DQ test (without its meta) keeps its own copy."""
    root = _project(tmp_path, PROJECT_YAML.replace(
        "      - name: customer_segment\n",
        "      - name: customer_segment\n        tests:\n          - accepted_values:\n"
        "              name: dq_accepted_values_dim_customers_customer_segment_c3\n              values: [high]\n"))
    _apply_dq_schema_tests(DQ_BLOCK, root)
    seg = next(c for c in _model(root)["columns"] if c["name"] == "customer_segment")
    assert _test_names(seg["tests"]) == ["dq_accepted_values_dim_customers_customer_segment_c3"]
