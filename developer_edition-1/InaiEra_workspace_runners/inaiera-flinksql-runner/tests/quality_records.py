"""Contract-v2 record builders for consumer tests."""
from __future__ import annotations

from app.core import quality_contract as qc
from app.core.config import get_settings


def window_result(**kw) -> dict:
    r = {
        "contract_version": qc.CONTRACT_VERSION, "record_type": "window_result",
        "org_id": get_settings().org_id, "ucs_fqn": "iceberg.silver.orders",
        "binding_id": "b-1", "artifact_version_id": "av-1", "check_id": "chk-1",
        "rule_id": "r-1", "rule_name": "amount_not_null", "rule_version": 3,
        "dimension": "nulls", "severity": "ERROR", "enforcement_mode": "warn",
        "breach_status": "fail", "threshold": 0.0, "column_name": "amount",
        "window_start": "2026-10-01T00:00:00", "window_end": "2026-10-01T00:01:00",
        "window_seconds": 60, "evaluated_rows": 100, "violation_count": 3,
        "sample_values": ["None", None, "None", "x" * 400], "run_ref": "dep-1",
        "parent_job_id": "6f1a3a52-6b55-4f53-8cf4-0d2c5b1f2a10",
        "emitted_at": "2026-10-01T00:01:01",
    }
    r.update(kw)
    return r


def gate_event(**kw) -> dict:
    base = {"record_type": "gate_event", "action": "quarantine", "enforcement_mode": "blocking"}
    base.update(kw)
    return window_result(**base)
