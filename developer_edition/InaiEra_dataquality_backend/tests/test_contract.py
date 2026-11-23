"""DQ-CONTRACT-005: one shared, versioned quality contract."""
import hashlib
import os
import pathlib

import pytest

from app.core import quality_contract as qc
from app.core.config import get_settings

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_contract_file_hash_matches_module():
    raw = (ROOT / "app" / "contracts" / "quality_contract.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == qc.CONTRACT_SHA256


def test_contract_basics():
    assert qc.CONTRACT_VERSION == "2.0"
    assert qc.RESULTS_TOPIC.endswith(".v2") and qc.GATE_TOPIC.endswith(".v2")
    assert "evaluated_rows" in qc.field_names("window_result")
    assert qc.field_names("gate_event")[-1] == "action"
    assert qc.RUN_STATUSES == ("started", "completed_pass", "completed_fail", "completed_warn",
                               "unknown", "stale")


def test_settings_inject_contract_defaults():
    s = get_settings()
    assert (s.quality_results_topic, s.quality_gate_topic, s.quality_quarantine_topic) == (
        qc.RESULTS_TOPIC, qc.GATE_TOPIC, qc.QUARANTINE_TOPIC)
    assert s.quality_contract_version == qc.CONTRACT_VERSION


def test_validate_record():
    rec = {f["name"]: None for f in qc.fields("window_result")}
    rec.update(contract_version="2.0", record_type="window_result", org_id="o", ucs_fqn="a.b",
               binding_id="b", artifact_version_id="a", check_id="c", rule_id="r",
               rule_name="n", rule_version=1, dimension="nulls", severity="ERROR",
               enforcement_mode="warn", breach_status="fail", threshold=0.0,
               window_start="2026-01-01T00:00:00", window_end="2026-01-01T00:01:00",
               window_seconds=60, evaluated_rows=10, violation_count=1, run_ref="d",
               emitted_at="2026-01-01T00:01:01")
    assert qc.validate_record(rec) == []
    assert qc.validate_record({**rec, "contract_version": "1.0"})
    assert qc.validate_record({**rec, "evaluated_rows": -1})
    assert qc.validate_record({**rec, "record_type": "x"})


def _sibling():
    env = os.environ.get("FLINK_RUNNER_PATH")
    candidates = [pathlib.Path(env)] if env else []
    candidates.append(ROOT.parent / "InaiEra_workspace_runners" / "inaiera-flinksql-runner")
    for c in candidates:
        if (c / "app" / "contracts" / "quality_contract.json").exists():
            return c
    return None


@pytest.mark.skipif(_sibling() is None, reason="FlinkSQL runner checkout not available "
                                               "(set FLINK_RUNNER_PATH)")
def test_contract_is_byte_identical_with_the_flinksql_runner():
    sib = _sibling()
    for rel in ("app/contracts/quality_contract.json", "app/core/quality_contract.py"):
        assert (sib / rel).read_bytes() == (ROOT / rel).read_bytes(), rel
