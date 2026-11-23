"""DQ-RESULT-010/011 — record -> QualityRun mapping (pure functions)."""
from __future__ import annotations

import pytest

from app.core import quality_contract as qc
from app.core.config import get_settings
from app.services import quality_consumer as c
from tests.quality_records import gate_event, window_result

S = get_settings()
RES, GATE = S.quality_results_topic, S.quality_gate_topic


@pytest.mark.parametrize("ev,vc,thr,breach,status", [
    (100, 0, 0.0, "fail", "completed_pass"), (100, 3, 0.05, "fail", "completed_pass"),
    (100, 3, 0.0, "fail", "completed_fail"), (100, 3, 0.0, "warn", "completed_warn"),
    (0, 0, 0.0, "fail", "unknown"),
])
def test_run_status(ev, vc, thr, breach, status):
    assert c.run_status(ev, vc, thr, breach) == status


def test_window_result_becomes_an_aggregated_quality_run():
    e = c.process_record(RES, 0, 7, window_result(), S)
    assert e.kind == "quality_run" and e.status == "pending" and e.heartbeat
    p = e.payload
    assert p["status"] == "completed_fail" and p["violation_rate"] == 0.03
    assert p["evaluated_rows"] == 100 and p["threshold"] == 0.0
    assert p["samples"] == ["None", "x" * qc.MAX_SAMPLE_CHARS]      # deduped, nulls dropped, truncated
    assert p["dataset_fqn"] == "iceberg.silver.orders" and p["rule_version"] == 3
    assert p["idempotency_key"] == e.key


def test_idempotency_key_is_semantic_not_offset_based():
    a = c.process_record(RES, 0, 7, window_result(), S)
    b = c.process_record(RES, 3, 9999, window_result(), S)          # Flink replay, new offset
    other = c.process_record(RES, 0, 8, window_result(window_end="2026-10-01T00:02:00"), S)
    assert a.key == b.key != other.key


def test_bounded_samples_never_exceed_contract_limit():
    e = c.process_record(RES, 0, 1, window_result(sample_values=[str(i) for i in range(50)]), S)
    assert len(e.payload["samples"]) == qc.MAX_SAMPLES


@pytest.mark.parametrize("record,why", [
    (window_result(contract_version="1.0"), "contract_version"),
    (window_result(org_id="other-org"), "data plane"),
    ({k: v for k, v in window_result().items() if k != "check_id"}, "check_id"),
    (window_result(evaluated_rows=-1), "evaluated_rows"),
    (window_result(record_type="gate_event"), "action"),
    (b"not json", "not JSON"),
])
def test_contract_violations_are_dead_lettered(record, why):
    e = c.process_record(RES, 0, 5, record, S)
    assert e.kind == "dead_letter" and e.status == "rejected" and why in e.error
    assert e.key == f"dead-letter:{RES}-0-5"


def test_record_on_wrong_topic_is_dead_lettered():
    e = c.process_record(GATE, 0, 1, window_result(), S)
    assert e.kind == "dead_letter"


def test_gate_event_mapping():
    e = c.process_record(GATE, 0, 1, gate_event(action="fail_stop"), S)
    assert e.kind == "gate_event" and e.payload["gate"] is True
    assert e.payload["action"] == "fail_stop" and e.payload["status"] == "completed_fail"


def test_legacy_mapping_only_for_completed_runs():
    p = c.process_record(RES, 0, 1, window_result(), S).payload
    legacy = c.legacy_result(p)
    assert legacy["status"] == "fail" and legacy["rows_scanned"] == 100
    assert legacy["pass_rate"] == 97.0 and legacy["observed"] == 0.03
    assert c.legacy_result({**p, "status": "stale"}) is None
