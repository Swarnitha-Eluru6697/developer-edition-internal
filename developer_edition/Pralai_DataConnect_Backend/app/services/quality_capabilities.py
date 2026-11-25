"""What a quality policy can actually do, as machine-readable availability (NEW-09): where failing records can go, and what can be done when a
record fails. See app/core/capability.py for the three states.

This is the ONE place that says which options exist. The policy validator (app/services/quality.py) refuses an `unavailable` choice from here, and
GET /quality-policies/capabilities hands the same answers to the UI, so a screen can disable an option for exactly the reason the API would reject it.
"""

from dataclasses import dataclass

from app.core.capability import (
    KAFKA_BOOTSTRAP_MISSING,
    KAFKA_REST_PROXY_URL_MISSING,
    NOT_IMPLEMENTED,
    SUPPORTED,
    Availability,
    requires_configuration,
    unavailable,
)
from app.core.config import settings


@dataclass(frozen=True)
class Option:
    id: str
    label: str
    description: str
    availability: Availability

    def as_dict(self) -> dict:
        return {"id": self.id, "label": self.label, "description": self.description, "availability": self.availability.as_dict()}


def _kafka_availability() -> Availability:
    """Quarantining to Kafka needs a way to produce: the native client needs a bootstrap server, the REST proxy backend needs its URL. Either
    missing is a deployment that is not set up for it — implemented, fixable by an operator, so requires_configuration, not unavailable."""
    if settings.kafka_producer_backend == "rest_proxy":
        if not (settings.kafka_rest_proxy_url or "").strip():
            return requires_configuration(
                KAFKA_REST_PROXY_URL_MISSING, "Quarantining to Kafka needs the Kafka REST proxy, and its URL is not set.",
                "Set DATACONNECT_KAFKA_REST_PROXY_URL (or switch DATACONNECT_KAFKA_PRODUCER_BACKEND to native).",
            )
    elif not (settings.kafka_bootstrap or "").strip():
        return requires_configuration(
            KAFKA_BOOTSTRAP_MISSING, "Quarantining to Kafka needs a Kafka bootstrap server, and none is set.",
            "Set DATACONNECT_KAFKA_BOOTSTRAP.",
        )
    return SUPPORTED


def quarantine_sinks() -> list[Option]:
    return [
        Option(
            "kafka", "Kafka topic",
            "Each failing record is written, in full, to the pipeline's validation quarantine topic.",
            _kafka_availability(),
        ),
        Option(
            "object_storage", "Object storage",
            "Each failing record is written to a bucket (S3, GCS, Azure).",
            unavailable(
                NOT_IMPLEMENTED, "Quarantining to object storage is reserved for a future backend and is not implemented yet.",
                "Use the Kafka quarantine topic.",
            ),
        ),
    ]


def failure_actions() -> list[Option]:
    kafka = _kafka_availability()
    return [
        Option(
            "route_to_quarantine", "Route to quarantine",
            "Failing records are copied to the quarantine destination and counted; in ENFORCE mode they never reach the prepared topic.",
            # Routing needs somewhere to route to: it is exactly as available as the Kafka destination it uses.
            kafka,
        ),
        Option(
            "log", "Log and count only",
            "Failing records are counted and logged. They are not copied anywhere, so nothing is kept for remediation.",
            SUPPORTED,
        ),
        Option(
            "drop", "Drop",
            "Discard failing records.",
            unavailable(
                NOT_IMPLEMENTED,
                "Dropping failing records is not implemented. DataConnect never silently discards a record that failed a rule.",
                "Use 'route_to_quarantine' to keep them, or 'log' to only count them.",
            ),
        ),
        Option(
            "block", "Block the stream",
            "Stop processing when a record fails.",
            unavailable(
                NOT_IMPLEMENTED,
                "Stopping a stream when a record fails is not implemented; it is a separate policy with its own consequences, not a variant of quarantine.",
                "Use 'route_to_quarantine' with quality mode ENFORCE to keep failing records out of downstream consumers.",
            ),
        ),
    ]


def quality_capabilities() -> dict:
    return {
        "quarantine_sinks": [o.as_dict() for o in quarantine_sinks()],
        "failure_actions": [o.as_dict() for o in failure_actions()],
    }


def _find(options: list[Option], option_id: str) -> Option | None:
    return next((o for o in options if o.id == option_id), None)


def quarantine_sink_availability(sink_id: str) -> Availability | None:
    """None for a value that is not a known quarantine destination at all."""
    option = _find(quarantine_sinks(), sink_id)
    return option.availability if option else None


def failure_action_availability(action_id: str) -> Availability | None:
    """None for a value that is not a known failure action at all."""
    option = _find(failure_actions(), action_id)
    return option.availability if option else None
