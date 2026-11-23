"""GAP-04: which bad-record policies a pipeline may use, by what its connector can actually do.

Kafka Connect's dead-letter queue exists ONLY for sink connectors (its `errors.deadletterqueue.*` settings are
not even defined on a source connector — verified against a live Connect worker). A source connector that hits
an unconvertible record under `errors.tolerance=all` just skips it and logs it. So "route to the dead-letter
queue" and "pause after N errors in a window" (which counts that queue) are sink-only; a source gets the two
honest options: stop on the first error, or skip and log.

    FAIL_FAST         errors.tolerance=none — the task fails on the first bad record.    source + sink
    SKIP_AND_LOG      errors.tolerance=all, logged, NOT kept anywhere else.              source + sink
    DLQ_AND_CONTINUE  errors.tolerance=all, bad record kept on the pipeline's DLQ topic. sink only
    THRESHOLD         DLQ_AND_CONTINUE + pause the pipeline past N errors per window.    sink only

Pure functions, no I/O: the schema, the validator, the connector config builder, the migration and the tests all
read the rules from here so they cannot drift apart.
"""

from typing import Any

FAIL_FAST = "FAIL_FAST"
SKIP_AND_LOG = "SKIP_AND_LOG"
DLQ_AND_CONTINUE = "DLQ_AND_CONTINUE"
THRESHOLD = "THRESHOLD"

ALL_POLICIES = frozenset({FAIL_FAST, SKIP_AND_LOG, DLQ_AND_CONTINUE, THRESHOLD})
#: Policies that keep the bad record on a dead-letter topic (and so need a sink connector).
DLQ_POLICIES = frozenset({DLQ_AND_CONTINUE, THRESHOLD})
#: errors.tolerance for each; anything unrecognized must fail CLOSED (stop), never silently tolerate.
TOLERANCE = {FAIL_FAST: "none", SKIP_AND_LOG: "all", DLQ_AND_CONTINUE: "all", THRESHOLD: "all"}

_LABELS = {
    FAIL_FAST: "Fail immediately",
    SKIP_AND_LOG: "Skip bad records and log them (no dead-letter queue)",
    DLQ_AND_CONTINUE: "Log and route to the dead-letter queue",
    THRESHOLD: "Dead-letter queue, but pause if too many pile up",
}


def label(policy: str) -> str:
    return _LABELS.get(policy, policy)


def is_sink(direction: str | None) -> bool:
    return (direction or "").upper() == "SINK"


def template_supports_dlq(template: dict[str, Any]) -> bool:
    """A template can use the dead-letter queue only if it is a SINK and hasn't been curated to opt out."""
    return bool(template.get("supports_dlq", True)) and is_sink(template.get("connector_type"))


def allowed_policies(template: dict[str, Any]) -> list[str]:
    """The policies this template may use, in the order a UI should offer them (first = the default)."""
    if template_supports_dlq(template):
        return [DLQ_AND_CONTINUE, FAIL_FAST, THRESHOLD, SKIP_AND_LOG]
    return [SKIP_AND_LOG, FAIL_FAST]


def default_policy(template: dict[str, Any]) -> str:
    """What a pipeline gets when none is chosen: a sink keeps its bad records; a source skips and logs (which is
    what an unset policy has always effectively done on a source, since it has no dead-letter queue)."""
    return allowed_policies(template)[0]


def rejection_reason(policy: str, template: dict[str, Any]) -> str:
    """Why `policy` isn't available for `template`, written for the person who chose it."""
    if not is_sink(template.get("connector_type")):
        return (
            f"{label(policy)!r} needs a dead-letter queue, and Kafka Connect only provides one for SINK "
            "connectors — a source connector can't route a bad record anywhere. Choose 'Fail immediately' or "
            "'Skip bad records and log them'."
        )
    return (
        f"{label(policy)!r} needs a dead-letter queue, which this connector template doesn't support. "
        "Choose 'Fail immediately' or 'Skip bad records and log them'."
    )
