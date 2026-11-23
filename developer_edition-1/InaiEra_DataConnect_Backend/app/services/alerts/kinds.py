"""What an alert rule can watch for, how its numbers are read, and the rules every organization starts with."""

from __future__ import annotations

from dataclasses import dataclass

SEVERITIES = ("low", "medium", "high")
SEVERITY_RANK = {s: i for i, s in enumerate(SEVERITIES)}

# How far back a counting rule looks. 0 = the default.
DEFAULT_WINDOW_SECONDS = 3_600
WINDOWS = (0, 300, 900, 3_600, 21_600, 86_400, 604_800)
# The longest window any rule can have: nothing older can matter.
LOOKBACK_SECONDS = max(WINDOWS)


@dataclass(frozen=True)
class RuleKind:
    label: str
    description: str
    threshold_hint: str
    uses_window: bool = True       # False: only the pipeline's current state matters (the threshold, if any, is a duration)
    uses_min_runs: bool = False    # kept from the Workspace shape; no DataConnect kind uses it
    max_threshold: int = 1_000
    default_on: bool = True        # is one of the rules every organization starts with


RULE_KINDS: dict[str, RuleKind] = {
    "pipeline_failed": RuleKind(
        "Pipeline failed",
        "The pipeline's connector, or one of its tasks, is FAILED in Kafka Connect (or no worker runs it).",
        "Not used: it fires while the pipeline is failed.",
        uses_window=False, max_threshold=1,
    ),
    "pipeline_degraded": RuleKind(
        "Pipeline degraded",
        "The connector is up but some of its tasks failed, or its quality check stopped processing records.",
        "Not used: it fires while the pipeline is degraded.",
        uses_window=False, max_threshold=1,
    ),
    "operation_failed": RuleKind(
        "Deploy or action failed",
        "A deploy, pause, resume or restart of the pipeline ended failed.",
        "Number of failed actions in the window. 1 = every failure.",
    ),
    "error_threshold_paused": RuleKind(
        "Paused by error threshold",
        "DataConnect paused the pipeline because its dead-letter errors passed the pipeline's error threshold.",
        "Number of such pauses in the window.",
    ),
    "dlq_errors": RuleKind(
        "Dead-letter errors",
        "Records Kafka Connect could not process were sent to the pipeline's dead-letter queue.",
        "Number of dead-letter records in the window.",
        max_threshold=1_000_000,
    ),
    "quality_failures": RuleKind(
        "Quality check failures",
        "Records failed the pipeline's quality check.",
        "Number of failing records in the window.",
        max_threshold=1_000_000,
    ),
    "runtime_unreachable": RuleKind(
        "Connector state unknown",
        "Kafka Connect could not tell the pipeline's state (unreachable or not answering) for a while.",
        "Minutes without a known state (1-1440).",
        uses_window=False, max_threshold=1_440, default_on=False,
    ),
    "schema_drift_pending": RuleKind(
        "Schema change waiting",
        "A schema change seen on the pipeline is waiting for approval.",
        "Hours it has been waiting (1-720).",
        uses_window=False, max_threshold=720, default_on=False,
    ),
}


@dataclass(frozen=True)
class DefaultRule:
    name: str
    kind: str
    severity: str
    threshold: int
    window_seconds: int = 0
    min_runs: int = 1


# Created once per organization (AlertService.ensure_defaults); an admin may edit or delete them afterwards and they do not come back.
# "Connector state unknown" and "Schema change waiting" are not among them: when Kafka Connect itself is down EVERY pipeline's state is
# unknown, which would raise one alert per pipeline; an organization that wants them adds them.
DEFAULT_RULES: tuple[DefaultRule, ...] = (
    DefaultRule("pipeline-failed", "pipeline_failed", "high", 1),
    DefaultRule("pipeline-degraded", "pipeline_degraded", "medium", 1),
    DefaultRule("deploy-failed", "operation_failed", "high", 1, 3_600),
    DefaultRule("error-threshold-paused", "error_threshold_paused", "high", 1, 3_600),
    DefaultRule("high-dlq-errors", "dlq_errors", "medium", 100, 3_600),
    DefaultRule("quality-failures", "quality_failures", "medium", 100, 3_600),
)

DIRECTION_LABEL = {"SOURCE": "source pipeline", "SINK": "sink pipeline"}


def window_of(window_seconds: int) -> int:
    return window_seconds or DEFAULT_WINDOW_SECONDS


def span_text(seconds: int) -> str:
    """'5 minutes', '1 hour', '24 hours', '7 days'."""
    if seconds < 3_600:
        n, unit = max(1, round(seconds / 60)), "minute"
    elif seconds < 86_400 * 2:
        n, unit = round(seconds / 3_600), "hour"
    else:
        n, unit = round(seconds / 86_400), "day"
    return f"{n} {unit}{'' if n == 1 else 's'}"
