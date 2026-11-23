"""Cross-repo checks against the FlinkSQL runner checkout (pure modules
only, loaded by path): DQ's compiled quality job is split and planned by
the runner's own parsers exactly as apply expects, and a record produced
in the shape of DQ's results sink validates against the shared contract."""
import importlib.util
import os
import pathlib

import pytest

from app.core import quality_contract as qc
from app.renderers.flink import flink_sinks, render_flink
from tests.helpers import TOPICS, flink_ctx, item, source_table

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _runner():
    env = os.environ.get("FLINK_RUNNER_PATH")
    cands = [pathlib.Path(env)] if env else []
    cands.append(ROOT.parent / "InaiEra_workspace_runners" / "inaiera-flinksql-runner")
    return next((c for c in cands if (c / "app" / "services" / "sql_plan_service.py").exists()), None)


RUNNER = _runner()
pytestmark = pytest.mark.skipif(RUNNER is None, reason="FlinkSQL runner checkout not available "
                                                       "(set FLINK_RUNNER_PATH)")


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, RUNNER / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("mode", ["warn", "blocking"])
def test_runner_plans_dq_artifact_with_exactly_the_contract_sinks(mode):
    plan_svc = _load("app/services/sql_plan_service.py", "runner_sql_plan")
    script = _load("app/core/sql_script.py", "runner_sql_script")
    sql, _ = render_flink([item("not_null", "order_id"), item("regex", "email",
                                                              params={"pattern": "@"})],
                          source=source_table(), ctx=flink_ctx(mode), topics=TOPICS)
    plan = plan_svc.extract_plan(sql)
    assert {t["name"] for t in plan["sinks"]} == set(flink_sinks(mode))
    for sink in plan["sinks"]:
        topic_key = flink_sinks(mode)[sink["name"]]
        assert sink["options"]["topic"] == TOPICS[topic_key]
        assert [c["name"] for c in sink["columns"]] == qc.field_names(
            {"results": "window_result", "gate": "gate_event",
             "quarantine": "quarantine_record"}[topic_key])
    stmts = script.split_sql_script(sql)
    kinds = [script.statement_kind(s) for s in stmts]
    assert kinds[-1] == "STATEMENT_SET" and kinds.count("STATEMENT_SET") == 1


def test_dq_sink_record_shape_validates_against_the_runner_contract():
    runner_qc = _load("app/core/quality_contract.py", "runner_quality_contract")
    assert runner_qc.CONTRACT_SHA256 == qc.CONTRACT_SHA256
    record = {f["name"]: None for f in runner_qc.fields("window_result")}
    record.update(contract_version=qc.CONTRACT_VERSION, record_type="window_result", org_id="o",
                  ucs_fqn="a.b.c", binding_id="b", artifact_version_id="a", check_id="c",
                  rule_id="r", rule_name="n", rule_version=1, dimension="nulls",
                  severity="ERROR", enforcement_mode="warn", breach_status="fail", threshold=0.0,
                  window_start="2026-01-01T00:00:00", window_end="2026-01-01T00:01:00",
                  window_seconds=60, evaluated_rows=5, violation_count=0, run_ref="d",
                  emitted_at="2026-01-01T00:01:00")
    assert runner_qc.validate_record(record) == []
