"""The alert rules, as pure functions: what the sweep has seen (observations) in, what is breaching out. No database, no network.

A rule breaches for ONE job at a time. What "breaching" means per kind (see kinds.py):

  run_failed            >= threshold failed runs in the window, and the job's latest run is one of them
  consecutive_failures  the last `threshold` runs all failed
  failure_rate          >= min_runs runs in the window and >= threshold percent of them failed
  quality_failed        like run_failed, but for runs that failed a data test / quality check
  upstream_blocked      >= threshold runs refused by Dagster in the window, and the latest thing that happened is a refusal
  flink_job_failed      the Flink job's latest state is failed

A cancelled run is not a failure. A run that is still queued / running is not looked at yet.

`reconcile` then decides what to do with the breaches: raise the new ones, leave the ones already reported alone (a channel is not
flooded with the same alert), and clear the alerts whose condition has stopped.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.services.alerts.kinds import JOB_KIND_LABEL, window_of, span_text

RUN_DONE = ("succeeded", "failed")


@dataclass(frozen=True)
class Obs:
    job_key: str
    job_name: str
    job_kind: str                  # dbt | sql | python | flink
    category: str                  # run | blocked | flink_state
    outcome: str                   # succeeded | failed | blocked | running | cancelled | ...
    occurred_at: datetime
    ref_id: str = ""
    failure_class: str | None = None    # "quality" for a failed data test
    detail: str | None = None


@dataclass(frozen=True)
class RuleSpec:
    id: str
    name: str
    kind: str
    severity: str
    threshold: int
    window_seconds: int
    min_runs: int
    scope_job_key: str | None
    enabled: bool = True


@dataclass(frozen=True)
class Breach:
    rule_id: str
    rule_name: str
    kind: str
    severity: str
    job_key: str
    job_name: str
    job_kind: str
    summary: str
    last_run_id: str | None

    @property
    def fingerprint(self) -> str:
        return f"{self.rule_id}:{self.job_key}"


REASON_LIMIT = 1_000


def reason_text(text: str | None, limit: int = REASON_LIMIT) -> str:
    """A failure's reason on one line: ALL of it (line breaks and runs of spaces become one space), so the alert says what actually went
    wrong ("Database Error failed to execute: HTTPConnectionPool(host='trino' ...") instead of only its first line. Cut, with "…", only
    past `limit` characters (the runners keep at most about 1000)."""
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def _label(o: Obs) -> str:
    return f"{JOB_KIND_LABEL.get(o.job_kind, 'job')} '{o.job_name}'"


def _within(items: Iterable[Obs], now: datetime, window: int) -> list[Obs]:
    cutoff = now - timedelta(seconds=window)
    return sorted((o for o in items if o.occurred_at >= cutoff), key=lambda o: o.occurred_at)


def _breach(rule: RuleSpec, last: Obs, summary: str) -> Breach:
    return Breach(rule.id, rule.name, rule.kind, rule.severity, last.job_key, last.job_name, last.job_kind, summary,
                  last.ref_id or None)


def evaluate_job(rule: RuleSpec, observations: Sequence[Obs], now: datetime) -> Breach | None:
    """The rule against one job's observations (any order). None = not breaching."""
    window = window_of(rule.window_seconds)
    if rule.kind == "flink_job_failed":
        states = sorted((o for o in observations if o.category == "flink_state"), key=lambda o: o.occurred_at)
        if states and states[-1].outcome == "failed":
            last = states[-1]
            return _breach(rule, last, f"{_label(last)} is in the failed state"
                                       + (f": {reason_text(last.detail)}" if reason_text(last.detail) else "."))
        return None

    runs = [o for o in _within((o for o in observations if o.category == "run" and o.outcome in RUN_DONE), now, window)]
    if rule.kind == "upstream_blocked":
        blocks = _within((o for o in observations if o.category == "blocked"), now, window)
        newest = max(blocks + runs, key=lambda o: o.occurred_at, default=None)
        if len(blocks) >= rule.threshold and newest is not None and newest.category == "blocked":
            n = len(blocks)
            return _breach(rule, newest, (f"A run of {_label(newest)} was refused" if n == 1 else
                                          f"{n} runs of {_label(newest)} were refused in the last {span_text(window)}")
                                         + (f": {reason_text(newest.detail)}" if reason_text(newest.detail) else "."))
        return None
    if not runs:
        return None
    latest = runs[-1]
    failed = [o for o in runs if o.outcome == "failed"]

    if rule.kind == "run_failed":
        plain = [o for o in failed if o.failure_class != "quality"]
        if latest.outcome == "failed" and latest.failure_class != "quality" and len(plain) >= rule.threshold:
            n = len(plain)
            reason = reason_text(latest.detail)
            return _breach(rule, latest, (f"{_label(latest)} failed" if n == 1 else
                                          f"{n} runs of {_label(latest)} failed in the last {span_text(window)}; the latest")
                                         + (f": {reason}" if reason else "."))
        return None
    if rule.kind == "quality_failed":
        bad = [o for o in failed if o.failure_class == "quality"]
        if latest.outcome == "failed" and latest.failure_class == "quality" and len(bad) >= rule.threshold:
            n = len(bad)
            reason = reason_text(latest.detail)
            return _breach(rule, latest, (f"A quality check failed on {_label(latest)}" if n == 1 else
                                          f"{n} runs of {_label(latest)} failed a quality check in the last {span_text(window)}")
                                         + (f": {reason}" if reason else "."))
        return None
    if rule.kind == "consecutive_failures":
        tail = runs[-rule.threshold:]
        if len(tail) >= rule.threshold and all(o.outcome == "failed" for o in tail):
            return _breach(rule, latest, f"The last {rule.threshold} runs of {_label(latest)} all failed"
                                         + (f"; the latest: {reason_text(latest.detail)}" if reason_text(latest.detail) else "."))
        return None
    if rule.kind == "failure_rate":
        total = len(runs)
        if total >= rule.min_runs and failed and len(failed) * 100 >= rule.threshold * total:
            pct = round(len(failed) * 100 / total)
            return _breach(rule, latest, f"{pct}% of {total} runs of {_label(latest)} failed in the last {span_text(window)} "
                                         f"(threshold {rule.threshold}%).")
        return None
    return None


def evaluate(rules: Iterable[RuleSpec], observations: Iterable[Obs], now: datetime) -> list[Breach]:
    """Every enabled rule against every job it applies to."""
    by_job: dict[str, list[Obs]] = {}
    for o in observations:
        by_job.setdefault(o.job_key, []).append(o)
    out: list[Breach] = []
    for rule in rules:
        if not rule.enabled:
            continue
        jobs = [rule.scope_job_key] if rule.scope_job_key else sorted(by_job)
        for key in jobs:
            b = evaluate_job(rule, by_job.get(key, []), now) if key else None
            if b is not None:
                out.append(b)
    return out


@dataclass(frozen=True)
class LiveAlert:
    id: str
    fingerprint: str
    state: str                     # open | acknowledged
    rule_id: str | None = None


@dataclass(frozen=True)
class Plan:
    raise_new: list[Breach]
    keep: list[tuple[LiveAlert, Breach]]      # still breaching: refresh the summary, tell nobody again
    clear: list[LiveAlert]                    # the condition stopped: resolve by itself


def reconcile(breaches: Sequence[Breach], live: Sequence[LiveAlert], *, evaluated_rule_ids: Iterable[str]) -> Plan:
    """What to do about the breaches given the alerts already open / acknowledged.

    Only alerts of rules that were evaluated this sweep are cleared: a rule that is disabled (or whose evaluation was skipped) leaves
    its alerts where they are instead of closing them behind someone's back."""
    live_by_fp = {a.fingerprint: a for a in live}
    seen: set[str] = set()
    raise_new: list[Breach] = []
    keep: list[tuple[LiveAlert, Breach]] = []
    for b in breaches:
        if b.fingerprint in seen:
            continue
        seen.add(b.fingerprint)
        existing = live_by_fp.get(b.fingerprint)
        if existing is None:
            raise_new.append(b)
        else:
            keep.append((existing, b))
    evaluated = set(evaluated_rule_ids)
    clear = [a for a in live if a.fingerprint not in seen and a.rule_id in evaluated]
    return Plan(raise_new, keep, clear)

