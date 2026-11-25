"""The alert rules, as pure functions: what the sweep has seen (observations) in, what is breaching out. No database, no network.

A rule breaches for ONE pipeline at a time. What "breaching" means per kind (see kinds.py):

  pipeline_failed         the pipeline's latest KNOWN state is failed
  pipeline_degraded       the pipeline's latest KNOWN state is degraded
  runtime_unreachable     the latest state is unknown, and has been for at least `threshold` minutes
  operation_failed        >= threshold failed deploys / pauses / resumes / restarts in the window, and the latest action is one of them
  error_threshold_paused  >= threshold error-threshold pauses in the window, and the pipeline has not been seen running since the latest
  dlq_errors              >= threshold dead-letter records in the window
  quality_failures        >= threshold records failing the quality check in the window
  schema_drift_pending    a schema change has been waiting for approval for at least `threshold` hours

Paused / deleted / never-deployed pipelines are not failures. An "unknown" state neither raises nor clears a failed / degraded alert: Kafka
Connect reports a connector it is restarting (the self-heal does that on every tick) without a state, and that is not a recovery.

`reconcile` then decides what to do with the breaches: raise the new ones, leave the ones already reported alone (a channel is not flooded
with the same alert), clear the alerts whose condition stopped, and — after a person resolved an alert by hand — raise that rule for that
pipeline again only on NEWER evidence, not because the same failure is still there.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.services.alerts.kinds import span_text, window_of

COUNT_CATEGORY = {"dlq_errors": "dlq", "quality_failures": "quality"}
STATE_OUTCOME = {"pipeline_failed": "failed", "pipeline_degraded": "degraded"}


@dataclass(frozen=True)
class Obs:
    pipeline_id: str
    pipeline_name: str
    direction: str                 # SOURCE | SINK
    category: str                  # state | operation | threshold | dlq | quality | drift
    outcome: str                   # failed | degraded | running | paused | unknown | succeeded | pending | count ...
    occurred_at: datetime          # UTC, timezone-aware
    ref_id: str = ""
    detail: str | None = None
    count: int = 1                 # dlq / quality: records in that one-minute bucket


@dataclass(frozen=True)
class RuleSpec:
    id: str
    name: str
    kind: str
    severity: str
    threshold: int
    window_seconds: int
    min_runs: int
    scope_pipeline_id: str | None
    enabled: bool = True


@dataclass(frozen=True)
class Breach:
    rule_id: str
    rule_name: str
    kind: str
    severity: str
    pipeline_id: str
    pipeline_name: str
    direction: str
    summary: str
    last_ref_id: str | None
    evidence_at: datetime          # when the newest thing behind this breach happened (a failed / degraded state: when it began)

    @property
    def fingerprint(self) -> str:
        return f"{self.rule_id}:{self.pipeline_id}"


REASON_LIMIT = 1_000


def reason_text(text: str | None, limit: int = REASON_LIMIT) -> str:
    """A failure's reason on one line: ALL of it (line breaks and runs of spaces become one space), so the alert says what actually went
    wrong instead of only its first words. Cut, with "…", only past `limit` characters."""
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def _label(o: Obs) -> str:
    return f"Pipeline '{o.pipeline_name}'"


def _with_reason(sentence: str, detail: str | None) -> str:
    reason = reason_text(detail)
    return f"{sentence}: {reason}" if reason else f"{sentence}."


def _within(items: Iterable[Obs], now: datetime, window: int) -> list[Obs]:
    cutoff = now - timedelta(seconds=window)
    return sorted((o for o in items if o.occurred_at >= cutoff), key=lambda o: o.occurred_at)


def _breach(rule: RuleSpec, last: Obs, summary: str, evidence_at: datetime | None = None) -> Breach:
    return Breach(rule.id, rule.name, rule.kind, rule.severity, last.pipeline_id, last.pipeline_name, last.direction, summary,
                  last.ref_id or None, evidence_at or last.occurred_at)


def _latest(observations: Sequence[Obs], category: str) -> Obs | None:
    of = [o for o in observations if o.category == category]
    return max(of, key=lambda o: o.occurred_at) if of else None


def _known_streak(observations: Sequence[Obs]) -> tuple[Obs, datetime] | None:
    """The newest state that is not "unknown", and when the pipeline entered it: the same state seen again after "unknown" readings (or
    with a new reason) is the same incident, so it is not newer evidence."""
    known = sorted((o for o in observations if o.category == "state" and o.outcome != "unknown"), key=lambda o: o.occurred_at)
    if not known:
        return None
    latest = known[-1]
    since = latest.occurred_at
    for o in reversed(known):
        if o.outcome != latest.outcome:
            break
        since = o.occurred_at
    return latest, since


def evaluate_pipeline(rule: RuleSpec, observations: Sequence[Obs], now: datetime) -> Breach | None:
    """The rule against one pipeline's observations (any order). None = not breaching."""
    kind = rule.kind
    if kind in STATE_OUTCOME:
        streak = _known_streak(observations)
        if streak is None or streak[0].outcome != STATE_OUTCOME[kind]:
            return None
        known, since = streak
        verb = "failed" if kind == "pipeline_failed" else "is degraded"
        return _breach(rule, known, _with_reason(f"{_label(known)} {verb}", known.detail), evidence_at=since)

    if kind == "runtime_unreachable":
        state = _latest(observations, "state")
        if state is None or state.outcome != "unknown":
            return None
        minutes = (now - state.occurred_at).total_seconds() / 60
        if minutes < rule.threshold:
            return None
        return _breach(rule, state, _with_reason(
            f"Kafka Connect has not reported the state of {_label(state).lower()} for {span_text(int(minutes * 60))}", state.detail))

    if kind == "schema_drift_pending":
        pending = [o for o in observations if o.category == "drift" and o.outcome == "pending"]
        if not pending:
            return None
        oldest = min(pending, key=lambda o: o.occurred_at)
        hours = (now - oldest.occurred_at).total_seconds() / 3_600
        if hours < rule.threshold:
            return None
        newest = max(pending, key=lambda o: o.occurred_at)
        n = len(pending)
        return _breach(rule, oldest, (f"A schema change seen on {_label(oldest).lower()} has waited for approval for "
                                      f"{span_text(int(hours * 3_600))}" if n == 1 else
                                      f"{n} schema changes seen on {_label(oldest).lower()} are waiting for approval, the oldest for "
                                      f"{span_text(int(hours * 3_600))}") + ".", evidence_at=newest.occurred_at)

    window = window_of(rule.window_seconds)

    if kind in COUNT_CATEGORY:
        buckets = [o for o in _within((o for o in observations if o.category == COUNT_CATEGORY[kind]), now, window) if o.count > 0]
        total = sum(o.count for o in buckets)
        if not buckets or total < rule.threshold:
            return None
        newest = buckets[-1]
        what = "records went to the dead-letter queue of" if kind == "dlq_errors" else "records failed the quality check of"
        return _breach(rule, newest, f"{total:,} {what} {_label(newest).lower()} in the last {span_text(window)} "
                                     f"(threshold {rule.threshold:,}).")

    if kind == "operation_failed":
        ops = _within((o for o in observations if o.category == "operation"), now, window)
        if not ops or ops[-1].outcome != "failed":
            return None
        failed = [o for o in ops if o.outcome == "failed"]
        if len(failed) < rule.threshold:
            return None
        latest = ops[-1]
        n = len(failed)
        return _breach(rule, latest, _with_reason(
            f"A {_op_name(latest)} of {_label(latest).lower()} failed" if n == 1 else
            f"{n} actions on {_label(latest).lower()} failed in the last {span_text(window)}; the latest, a {_op_name(latest)}",
            latest.detail))

    if kind == "error_threshold_paused":
        pauses = _within((o for o in observations if o.category == "threshold"), now, window)
        if len(pauses) < rule.threshold:
            return None
        latest = pauses[-1]
        running_since = [o for o in observations if o.category == "state" and o.outcome == "running" and o.occurred_at > latest.occurred_at]
        if running_since:
            return None                                   # resumed and running again: the pause was dealt with
        n = len(pauses)
        return _breach(rule, latest, _with_reason(
            f"DataConnect paused {_label(latest).lower()} because its dead-letter errors passed its error threshold" if n == 1 else
            f"DataConnect paused {_label(latest).lower()} {n} times in the last {span_text(window)} because its dead-letter errors passed "
            f"its error threshold", latest.detail))
    return None


_OP_NAMES = {"pipeline_deploy": "deploy", "pipeline_pause": "pause", "pipeline_resume": "resume", "pipeline_restart": "restart"}


def _op_name(o: Obs) -> str:
    """The action an operation observation is about; its ref_id is "<operation id>|<operation type>"."""
    return _OP_NAMES.get(o.ref_id.rpartition("|")[2], "action")


def evaluate(rules: Iterable[RuleSpec], observations: Iterable[Obs], now: datetime) -> list[Breach]:
    """Every enabled rule against every pipeline it applies to."""
    by_pipeline: dict[str, list[Obs]] = {}
    for o in observations:
        by_pipeline.setdefault(o.pipeline_id, []).append(o)
    out: list[Breach] = []
    for rule in rules:
        if not rule.enabled:
            continue
        pipelines = [rule.scope_pipeline_id] if rule.scope_pipeline_id else sorted(by_pipeline)
        for pid in pipelines:
            b = evaluate_pipeline(rule, by_pipeline.get(pid, []), now) if pid else None
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
    held_back: list[Breach]                   # resolved by a person and nothing newer has happened since: not raised again


def reconcile(
    breaches: Sequence[Breach], live: Sequence[LiveAlert], *, evaluated_rule_ids: Iterable[str],
    resolved_by_hand: Mapping[str, datetime] | None = None,
) -> Plan:
    """What to do about the breaches given the alerts already open / acknowledged.

    Only alerts of rules that were evaluated this sweep are cleared: a rule that is disabled (or whose evaluation was skipped) leaves its
    alerts where they are instead of closing them behind someone's back.

    `resolved_by_hand`: fingerprint -> when a person last resolved that rule's alert for that pipeline. Such a breach is raised again only if
    its evidence is newer than that (a new failure), so resolving an alert while its cause is still there does not bring the same alert
    straight back on the next sweep."""
    live_by_fp = {a.fingerprint: a for a in live}
    silenced = dict(resolved_by_hand or {})
    seen: set[str] = set()
    raise_new: list[Breach] = []
    keep: list[tuple[LiveAlert, Breach]] = []
    held: list[Breach] = []
    for b in breaches:
        if b.fingerprint in seen:
            continue
        seen.add(b.fingerprint)
        existing = live_by_fp.get(b.fingerprint)
        if existing is not None:
            keep.append((existing, b))
        elif b.fingerprint in silenced and b.evidence_at <= silenced[b.fingerprint]:
            held.append(b)
        else:
            raise_new.append(b)
    evaluated = set(evaluated_rule_ids)
    clear = [a for a in live if a.fingerprint not in seen and a.rule_id in evaluated]
    return Plan(raise_new, keep, clear, held)
