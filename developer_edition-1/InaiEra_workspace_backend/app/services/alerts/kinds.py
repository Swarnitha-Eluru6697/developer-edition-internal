"""What an alert rule can watch for, how its numbers are read, and the rules every org starts with."""
from __future__ import annotations

from dataclasses import dataclass

SEVERITIES = ("low", "medium", "high")
SEVERITY_RANK = {s: i for i, s in enumerate(SEVERITIES)}

# How far back a rule looks. 0 = the default.
DEFAULT_WINDOW_SECONDS = 86_400
WINDOWS = (0, 300, 900, 3_600, 21_600, 86_400, 604_800)


@dataclass(frozen=True)
class RuleKind:
    label: str
    description: str
    threshold_hint: str
    uses_window: bool = True       # false: only the job's current state matters
    uses_min_runs: bool = False
    max_threshold: int = 1_000


RULE_KINDS: dict[str, RuleKind] = {
    "run_failed": RuleKind(
        "Run failed",
        "A run of a dbt, SQL or Python job ended failed (and it is still the job's latest result).",
        "Number of failed runs in the window. 1 = every failure.",
    ),
    "consecutive_failures": RuleKind(
        "Repeated failures",
        "A job's last runs ALL failed: it is not a one-off.",
        "How many runs in a row.",
        max_threshold=100,
    ),
    "failure_rate": RuleKind(
        "Failure rate",
        "Too large a share of a job's runs failed in the window.",
        "Percent of runs failed (1-100).",
        uses_min_runs=True,
        max_threshold=100,
    ),
    "quality_failed": RuleKind(
        "Quality check failed",
        "A run failed because a data test or quality check did not pass.",
        "Number of such failed runs in the window.",
    ),
    "upstream_blocked": RuleKind(
        "Upstream blocked",
        "Dagster refused to start a run because a job above it failed, is stale, or has not run yet.",
        "Number of refused runs in the window.",
    ),
    "flink_job_failed": RuleKind(
        "Flink job failed",
        "A FlinkSQL job is in the failed state.",
        "Not used: it fires while the job is failed.",
        uses_window=False,
        max_threshold=1,
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


# Created once per org (see AlertService.ensure_defaults); an admin may edit or delete them afterwards.
DEFAULT_RULES: tuple[DefaultRule, ...] = (
    DefaultRule("job-run-failed", "run_failed", "high", 1, 3_600),
    DefaultRule("repeated-failures", "consecutive_failures", "high", 3),
    DefaultRule("high-failure-rate", "failure_rate", "medium", 50, 86_400, 5),
    DefaultRule("quality-check-failed", "quality_failed", "medium", 1, 86_400),
    DefaultRule("upstream-blocked", "upstream_blocked", "medium", 1, 3_600),
    DefaultRule("flink-job-failed", "flink_job_failed", "high", 1),
)

JOB_KIND_LABEL = {"dbt": "dbt project", "sql": "SQL job", "python": "Python job", "flink": "Flink job"}


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
