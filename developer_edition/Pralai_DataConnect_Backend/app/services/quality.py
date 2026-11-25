import re
from datetime import UTC, datetime
from typing import Any

from app.core.capability import CapabilityState
from app.core.config import settings
from app.services.quality_capabilities import (
    failure_action_availability,
    quarantine_sink_availability,
)

# Which options exist, and whether each can be executed, is decided in ONE place: app/services/quality_capabilities.py (NEW-09). The database also
# refuses a quarantine sink without a backend (ck_dc_quality_policies_quarantine_sink_implemented); keep that constraint in step with the options that
# are not `unavailable` there. A reserved or unimplemented option is refused here, at policy create/update time, so the rejection happens before a
# policy is ever saved — not silently accepted only to blow up the first time a real batch quarantines a failed record.
QUARANTINE_SINKS = {"kafka"}


def validate_quality_policy_fields(policy: dict[str, Any]) -> list[tuple[str, str]]:
    """Same checks as validate_quality_policy(), but each error paired with
    the field it's about — (field, message) — e.g. "rules[2].pattern" for a
    bad regex, so a caller can report field-level errors (see
    QualityRuleError's field_errors) instead of only a flattened message."""
    return [(field, message) for field, message, _code in validate_quality_policy_field_codes(policy)]


def validate_quality_policy_field_codes(policy: dict[str, Any]) -> list[tuple[str, str, str | None]]:
    """(field, message, code) — `code` is the machine-readable reason (NOT_IMPLEMENTED, ...) for a choice that is known but cannot be executed,
    None for an ordinary invalid value."""
    errors: list[tuple[str, str, str | None]] = []
    errors.extend(_capability_errors("quarantine_sink", policy.get("quarantine_sink", "kafka"), quarantine_sink_availability))
    errors.extend(_capability_errors("on_failure", policy.get("on_failure", "route_to_quarantine"), failure_action_availability))
    return errors + [(field, message, None) for field, message in _rule_errors(policy)]


def _capability_errors(field: str, value: Any, availability_of: Any) -> list[tuple[str, str, str | None]]:
    availability = availability_of(value) if isinstance(value, str) else None
    if availability is None:
        return [(field, f"Unsupported {field}: {value}", None)]
    if availability.state is CapabilityState.UNAVAILABLE:
        hint = f" {availability.remediation}" if availability.remediation else ""
        return [(field, f"{field}={value!r} is not available: {availability.message}{hint}", availability.code)]
    return []   # supported, or requires_configuration (implemented; an operator can set it up — the policy itself is valid)


def _rule_errors(policy: dict[str, Any]) -> list[tuple[str, str]]:
    errors: list[tuple[str, str]] = []
    for i, rule in enumerate(policy.get("rules", [])):
        rule_type = rule.get("type")
        if rule_type not in {
            "required",
            "type_check",
            "regex",
            "allowed_values",
            "min_max_numeric",
            "min_max_string_length",
            "timestamp_freshness",
            "null_percentage_threshold",
            "duplicate_key_batch",
            "referential_hint",
        }:
            errors.append((f"rules[{i}].type", f"Unsupported rule type: {rule_type}"))
        if rule_type == "regex":
            try:
                re.compile(rule.get("pattern", ""))
            except re.error as exc:
                errors.append((f"rules[{i}].pattern", f"Invalid regex for {rule.get('name')}: {exc}"))
    return errors


def validate_quality_policy(policy: dict[str, Any]) -> dict[str, Any]:
    errors = [message for _field, message in validate_quality_policy_fields(policy)]
    return {"valid": not errors, "errors": errors}


def evaluate_record(record: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    failed_rules: list[str] = []
    for rule in policy.get("rules", []):
        field = rule.get("field")
        value = record.get(field)
        if rule["type"] == "required" and value in (None, ""):
            failed_rules.append(rule["name"])
        elif rule["type"] == "type_check" and value is not None:
            if not _matches_type(value, rule.get("expected_type")):
                failed_rules.append(rule["name"])
        elif rule["type"] == "regex" and value is not None:
            if not re.fullmatch(rule["pattern"], str(value)):
                failed_rules.append(rule["name"])
        elif rule["type"] == "allowed_values" and value not in rule.get("values", []):
            failed_rules.append(rule["name"])
        elif rule["type"] == "min_max_numeric" and value is not None:
            try:
                number = float(value)
            except (TypeError, ValueError):
                failed_rules.append(rule["name"])
                continue
            minimum = rule.get("min")
            maximum = rule.get("max")
            if (minimum is not None and number < minimum) or (maximum is not None and number > maximum):
                failed_rules.append(rule["name"])
        elif rule["type"] == "min_max_string_length" and value is not None:
            length = len(str(value))
            minimum = rule.get("min")
            maximum = rule.get("max")
            if (minimum is not None and length < minimum) or (maximum is not None and length > maximum):
                failed_rules.append(rule["name"])
    return {"valid": not failed_rules, "failed_rules": failed_rules}


def evaluate_batch(records: list[dict[str, Any]], policy: dict[str, Any]) -> dict[str, Any]:
    evaluated = [evaluate_record(record, policy) for record in records]
    failed = [result for result in evaluated if not result["valid"]]
    return {
        "valid_count": len(records) - len(failed),
        "failed_count": len(failed),
        "total_count": len(records),
        "record_results": evaluated,
    }


def build_quarantine_envelope(
    *,
    original_topic: str,
    pipeline_id: str,
    schema_id: str | None,
    quality_policy_id: str | None,
    failed_rules: list[str],
    original_record: dict[str, Any],
    failure_type: str = "quality_rule_failed",
    original_partition: int | None = None,
    original_offset: int | None = None,
    event_id: str | None = None,
) -> dict[str, Any]:
    """`original_topic` is the topic the record was READ from. A pipeline can read several (a sink with more than one input topic), so the
    partition and offset are kept too, when known: with them a failure points at one exact message in one exact topic. `event_id` (NEW-05) is
    that position as one stable identity — `<topic>:<partition>:<offset>` — the same on every delivery of the same source record, so a consumer can
    recognise a repeat."""
    origin = {k: v for k, v in (("original_partition", original_partition), ("original_offset", original_offset), ("event_id", event_id)) if v is not None}
    return {
        **origin,
        "original_topic": original_topic,
        "pipeline_id": pipeline_id,
        "schema_id": schema_id,
        "quality_policy_id": quality_policy_id,
        "failure_type": failure_type,
        "failed_rules": failed_rules,
        "received_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "original_record": original_record,
    }


def build_quality_result_summary(
    *,
    org_id: str,
    pipeline_id: str,
    quality_policy_id: str,
    topic_name: str,
    records: list[dict[str, Any]],
    policy: dict[str, Any],
    sample_limit: int | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Returns (summary, quarantine_envelopes).

    `summary` is what gets persisted to dc_quality_results: aggregates plus a
    bounded sample of failing records — never the full failure list, so a
    high-volume batch can't turn the row's JSONB sample_failures column into
    an unbounded blob. `sample_limit` caps it; the default (None) uses
    settings.quality_max_sample_failures. A preview call (already bounded to
    a handful of records and never persisted) can pass an explicit larger
    limit to show every failure instead.

    `quarantine_envelopes` is every failing record, unbounded, for the caller
    to route through the policy's quarantine_sink (see
    app/services/quarantine_sink.py) — it is transient and must never be
    persisted as-is.
    """
    batch = evaluate_batch(records, policy)
    quarantine_envelopes = []
    for record, result in zip(records, batch["record_results"], strict=False):
        if not result["valid"]:
            quarantine_envelopes.append(
                build_quarantine_envelope(
                    original_topic=topic_name,
                    pipeline_id=pipeline_id,
                    schema_id=None,
                    quality_policy_id=quality_policy_id,
                    failed_rules=result["failed_rules"],
                    original_record=record,
                )
            )
    limit = settings.quality_max_sample_failures if sample_limit is None else sample_limit
    summary = {
        "org_id": org_id,
        "pipeline_id": pipeline_id,
        "quality_policy_id": quality_policy_id,
        "topic_name": topic_name,
        "rule_name": "*",
        "severity": "error",
        "status": "passed" if batch["failed_count"] == 0 else "failed",
        "failed_count": batch["failed_count"],
        "total_count": batch["total_count"],
        "sample_failures": quarantine_envelopes[:limit],
    }
    return summary, quarantine_envelopes


def _matches_type(value: Any, expected_type: str | None) -> bool:
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if expected_type == "boolean":
        return isinstance(value, bool)
    if expected_type == "object":
        return isinstance(value, dict)
    if expected_type == "array":
        return isinstance(value, list)
    return True
