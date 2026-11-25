"""Shared, versioned DQ quality-result contract (DQ-CONTRACT-005).

ONE document — app/contracts/quality_contract.json — defines the Kafka
topics and the exact record shapes exchanged between:

  producer  inaiera-data-quality   renders the FlinkSQL quality job whose
                                   sinks write these records
  consumer  pralai-flinksql-runner quality_consumer reads them, aggregates
                                   QualityRuns and delivers them to UCS

THIS MODULE AND THE JSON FILE MUST STAY BYTE-IDENTICAL BETWEEN THE TWO
REPOS (each repo has a test that pins CONTRACT_SHA256, and DQ's suite also
compares against the sibling checkout when it is available).

Both services inject the topics through their own Settings (defaulting to
the values here) and refuse to start when the configured
QUALITY_CONTRACT_VERSION differs from CONTRACT_VERSION. Every record also
carries `contract_version`, and the consumer dead-letters any record whose
major version it does not implement — so a deploy of one side without the
other is detected instead of silently mis-parsed.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contracts" / "quality_contract.json"
_RAW = CONTRACT_PATH.read_bytes()
CONTRACT: dict[str, Any] = json.loads(_RAW)
CONTRACT_SHA256 = hashlib.sha256(_RAW).hexdigest()

CONTRACT_NAME: str = CONTRACT["name"]
CONTRACT_VERSION: str = CONTRACT["version"]
RESULTS_TOPIC: str = CONTRACT["topics"]["results"]
GATE_TOPIC: str = CONTRACT["topics"]["gate"]
QUARANTINE_TOPIC: str = CONTRACT["topics"]["quarantine"]
KAFKA_SINK_OPTIONS: dict[str, str] = dict(CONTRACT["kafka_sink_options"])
RUN_STATUSES: tuple[str, ...] = tuple(CONTRACT["enums"]["quality_run_status"])
BREACH_STATUSES: tuple[str, ...] = tuple(CONTRACT["enums"]["breach_status"])
GATE_ACTIONS: tuple[str, ...] = tuple(CONTRACT["enums"]["gate_action"])
MAX_SAMPLES: int = int(CONTRACT["quality_run"]["max_samples"])
MAX_SAMPLE_CHARS: int = int(CONTRACT["quality_run"]["max_sample_chars"])
STALE_AFTER_WINDOWS: int = int(CONTRACT["quality_run"]["stale_after_windows"])
AGGREGATION_KEY: tuple[str, ...] = tuple(CONTRACT["quality_run"]["aggregation_key"])
RECORD_TYPES: tuple[str, ...] = tuple(CONTRACT["records"])


def major(version: str) -> str:
    return str(version).split(".", 1)[0]


def is_compatible_version(version: str | None) -> bool:
    return version is not None and major(version) == major(CONTRACT_VERSION)


def fields(record_type: str) -> list[dict[str, Any]]:
    """Ordered field specs for a record type (resolving `extends`)."""
    spec = CONTRACT["records"][record_type]
    out: list[dict[str, Any]] = []
    if spec.get("extends"):
        out.extend(fields(spec["extends"]))
    out.extend(spec["fields"])
    return out


def field_names(record_type: str) -> list[str]:
    return [f["name"] for f in fields(record_type)]


def topic_key(record_type: str) -> str:
    return CONTRACT["records"][record_type]["topic"]


def _type_ok(json_type: str, value: Any) -> bool:
    if value is None:
        return True
    if json_type == "string":
        return isinstance(value, str)
    if json_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if json_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if json_type == "array":
        return isinstance(value, list)
    if json_type == "timestamp":
        if not isinstance(value, str):
            return False
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
        return True
    return False


def validate_record(record: Any) -> list[str]:
    """Contract violations for one decoded Kafka record ([] == valid)."""
    if not isinstance(record, dict):
        return ["record is not a JSON object"]
    errors: list[str] = []
    rtype = record.get("record_type")
    if rtype not in CONTRACT["records"]:
        return [f"unknown record_type {rtype!r}"]
    if not is_compatible_version(record.get("contract_version")):
        errors.append(f"contract_version {record.get('contract_version')!r} is not compatible "
                      f"with {CONTRACT_VERSION!r}")
    for f in fields(rtype):
        name = f["name"]
        if f["required"] and record.get(name) in (None, ""):
            errors.append(f"missing required field {name!r}")
        elif name in record and not _type_ok(f["json_type"], record[name]):
            errors.append(f"field {name!r} is not of type {f['json_type']}")
    if rtype in ("window_result", "gate_event"):
        if record.get("breach_status") not in BREACH_STATUSES:
            errors.append(f"breach_status must be one of {list(BREACH_STATUSES)}")
        for n in ("evaluated_rows", "violation_count"):
            v = record.get(n)
            if isinstance(v, int) and v < 0:
                errors.append(f"{n} must be >= 0")
    if rtype == "gate_event" and record.get("action") not in GATE_ACTIONS:
        errors.append(f"action must be one of {list(GATE_ACTIONS)}")
    return errors
