from enum import StrEnum
from typing import Any


class DriftAction(StrEnum):
    ALLOW_NOTIFY = "allow_and_notify"
    HOLD_APPROVAL = "hold_for_approval"
    ALERT = "alert"
    QUARANTINE = "quarantine"
    BLOCK_DEPLOYMENT = "block_deployment"
    SCHEMA_REVIEW = "route_to_schema_review"


def compare_schema_fields(
    approved_fields: list[dict[str, Any]], observed_fields: list[dict[str, Any]]
) -> dict[str, Any]:
    approved = {field["name"]: field for field in approved_fields}
    observed = {field["name"]: field for field in observed_fields}
    events: list[dict[str, Any]] = []

    for name, observed_field in observed.items():
        if name not in approved:
            nullable = observed_field.get("nullable", True)
            events.append(
                {
                    "field": name,
                    "drift_type": "new_optional_field" if nullable else "new_required_field",
                    "action": DriftAction.ALLOW_NOTIFY if nullable else DriftAction.HOLD_APPROVAL,
                    "details": {"observed": observed_field},
                }
            )
            continue

        approved_field = approved[name]
        # A contract that leaves primary_key out means false: None != False made every such field look "changed".
        if bool(approved_field.get("primary_key")) != bool(observed_field.get("primary_key")):
            events.append(
                {
                    "field": name,
                    "drift_type": "primary_key_changed",
                    "action": DriftAction.BLOCK_DEPLOYMENT,
                    "details": {"approved": approved_field, "observed": observed_field},
                }
            )
        elif approved_field.get("type") != observed_field.get("type") and not (
            # Records give "integer" for a whole number (10, 2) and "number" for one with decimals (10.5): a whole value in a number column is
            # still a number, not a type change. (The other way round — decimals in an integer column — is a real change and still reported.)
            approved_field.get("type") == "number" and observed_field.get("type") == "integer"
        ):
            if "object" in {approved_field.get("type"), observed_field.get("type")}:
                action = DriftAction.SCHEMA_REVIEW
                drift_type = "nested_structure_changed"
            else:
                action = DriftAction.QUARANTINE
                drift_type = "type_changed"
            events.append(
                {
                    "field": name,
                    "drift_type": drift_type,
                    "action": action,
                    "details": {"approved": approved_field, "observed": observed_field},
                }
            )

    for name, approved_field in approved.items():
        if name not in observed:
            events.append(
                {
                    "field": name,
                    "drift_type": "field_removed",
                    "action": DriftAction.ALERT,
                    "details": {"approved": approved_field},
                }
            )

    blocking_actions = {DriftAction.BLOCK_DEPLOYMENT, DriftAction.HOLD_APPROVAL}
    return {
        "compatible": not any(event["action"] in blocking_actions for event in events),
        "events": events,
    }


def approval_required(events: list[dict[str, Any]]) -> bool:
    approval_actions = {DriftAction.HOLD_APPROVAL, DriftAction.BLOCK_DEPLOYMENT}
    return any(event["action"] in approval_actions for event in events)
