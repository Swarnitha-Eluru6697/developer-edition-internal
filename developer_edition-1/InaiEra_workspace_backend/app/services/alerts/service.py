"""The database side of alerts: rules and channels (CRUD), the alert lifecycle (acknowledge / resolve) and the sweep.

One sweep, every minute (sweeper.py) or on demand ("Run rules now"):

  1. take the sweep lease (one sweep at a time across replicas)         6. raise the new breaches, keep the reported ones, clear the ones
  2. create the default rules, once per org                                that stopped (engine.reconcile)
  3. ask the sources what happened (sources.py), each on its own        7. deliver every alert that is not yet sent to the channels that
  4. remember it (alert_observations), minus what was already seen        match it (retrying the failed ones, a few times)
  5. evaluate the enabled rules over what is remembered                  8. forget old observations; release the lease

An alert is raised once per breach and stays quiet while it is open or acknowledged; it is cleared automatically when its condition
stops. Acknowledging / resolving by hand is done in the UI (a Slack / Teams webhook is one-way, so nothing is acted on from there).
"""
from __future__ import annotations

import dataclasses
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable

import structlog
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import get_settings
from app.core.errors import conflict, flush_or_conflict, not_found, validation_failed
from app.models import Alert, AlertChannel, AlertEvent, AlertObservation, AlertRule, AlertState
from app.services.alerts import links, sources, webhooks
from app.services.alerts.engine import Breach, LiveAlert, Obs, RuleSpec, evaluate, reconcile
from app.services.alerts.kinds import (
    DEFAULT_RULES, JOB_KIND_LABEL, RULE_KINDS, SEVERITY_RANK, WINDOWS,
)
from app.storage import get_secrets_backend

log = structlog.get_logger()

SWEEP_ACTOR = "rule sweep"
SYSTEM_ACTOR = "system"
LEASE_SECONDS = 120
LOOKBACK_SECONDS = 604_800            # the longest rule window: observations older than this cannot matter to any rule


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def aware(dt: datetime) -> datetime:
    """SQLite (tests) hands back naive datetimes; Postgres aware ones. Everything compared here is UTC."""
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def secret_path(channel_id: uuid.UUID) -> str:
    return f"alerts/channels/{channel_id}"


# ── validation ─────────────────────────────────────────────────────────────────────────────────────────────────────────────
def validate_rule_numbers(kind: str, threshold: int, window_seconds: int, min_runs: int) -> None:
    info = RULE_KINDS[kind]
    if threshold > info.max_threshold:
        raise validation_failed("threshold", f"The threshold of a '{info.label}' rule can be at most {info.max_threshold}.")
    if window_seconds not in WINDOWS:
        raise validation_failed("window_seconds", "Pick one of the offered windows.")
    if not info.uses_window and window_seconds:
        raise validation_failed("window_seconds", f"A '{info.label}' rule has no window: it fires while the job is in that state.")
    if not info.uses_min_runs and min_runs != 1:
        raise validation_failed("min_runs", f"A '{info.label}' rule has no minimum number of runs.")


def _spec(r: AlertRule) -> RuleSpec:
    return RuleSpec(str(r.id), r.name, r.kind, r.severity, r.threshold, r.window_seconds, r.min_runs, r.scope_job_key, r.enabled)


@dataclass
class SweepSummary:
    ran: bool
    detail: str = ""
    raised: int = 0
    cleared: int = 0
    delivered: int = 0
    sources: list[str] = field(default_factory=list)


class AlertService:
    def __init__(self, db: AsyncSession, org_id: str | None = None):
        self.db = db
        self.org_id = org_id or get_settings().org_id

    # ── rules ──────────────────────────────────────────────────────────────────────────────────────────────────────────────
    async def ensure_defaults(self) -> None:
        """The rules every org starts with, created once (an admin may change or delete them; they do not come back)."""
        state = await self._state()
        if state.defaults_seeded:
            return
        existing = set((await self.db.scalars(select(AlertRule.name).where(AlertRule.org_id == self.org_id))).all())
        for d in DEFAULT_RULES:
            if d.name not in existing:
                self.db.add(AlertRule(org_id=self.org_id, name=d.name, kind=d.kind, severity=d.severity, threshold=d.threshold,
                                      window_seconds=d.window_seconds, min_runs=d.min_runs, enabled=True, created_by=SYSTEM_ACTOR))
        state.defaults_seeded = True
        await self.db.flush()

    async def _state(self) -> AlertState:
        state = await self.db.get(AlertState, self.org_id)
        if state is None:
            state = AlertState(org_id=self.org_id, defaults_seeded=False)
            self.db.add(state)
            await self.db.flush()
        return state

    async def list_rules(self) -> list[AlertRule]:
        await self.ensure_defaults()
        return list((await self.db.scalars(select(AlertRule).where(AlertRule.org_id == self.org_id).order_by(AlertRule.created_at, AlertRule.name))).all())

    async def get_rule(self, rule_id: uuid.UUID) -> AlertRule:
        rule = await self.db.get(AlertRule, rule_id)
        if rule is None or rule.org_id != self.org_id:
            raise not_found("alert_rule", str(rule_id))
        return rule

    async def create_rule(self, *, actor: str, **f) -> AlertRule:
        validate_rule_numbers(f["kind"], f["threshold"], f["window_seconds"], f["min_runs"])
        await self.ensure_defaults()
        if await self._name_taken(AlertRule, f["name"]):
            raise conflict(f"A rule named '{f['name']}' already exists.", code="ALERT_RULE_NAME_TAKEN", resource="alert_rule", field="name")
        rule = AlertRule(org_id=self.org_id, created_by=actor, **f)
        self.db.add(rule)
        await flush_or_conflict(self.db, f"A rule named '{f['name']}' already exists.", constraint="uq_alert_rule_org_name")
        return rule

    async def update_rule(self, rule_id: uuid.UUID, changes: dict) -> AlertRule:
        rule = await self.get_rule(rule_id)
        if "name" in changes and changes["name"] != rule.name and await self._name_taken(AlertRule, changes["name"], exclude=rule.id):
            raise conflict(f"A rule named '{changes['name']}' already exists.", code="ALERT_RULE_NAME_TAKEN", resource="alert_rule", field="name")
        merged = {k: changes.get(k, getattr(rule, k)) for k in ("kind", "threshold", "window_seconds", "min_runs")}
        # Changing to a kind without a window / minimum resets the fields it does not use rather than failing on stale values.
        info = RULE_KINDS[merged["kind"]]
        if not info.uses_window and "window_seconds" not in changes:
            merged["window_seconds"] = 0
        if not info.uses_min_runs and "min_runs" not in changes:
            merged["min_runs"] = 1
        if not info.max_threshold >= merged["threshold"] and "threshold" not in changes:
            merged["threshold"] = info.max_threshold
        validate_rule_numbers(merged["kind"], merged["threshold"], merged["window_seconds"], merged["min_runs"])
        for k, v in {**changes, **merged}.items():
            setattr(rule, k, v)
        await flush_or_conflict(self.db, "A rule with this name already exists.", constraint="uq_alert_rule_org_name")
        await self.db.refresh(rule)                       # updated_at is set by the database on UPDATE
        return rule

    async def delete_rule(self, rule_id: uuid.UUID) -> None:
        await self.db.delete(await self.get_rule(rule_id))
        await self.db.flush()

    async def _name_taken(self, model, name: str, exclude: uuid.UUID | None = None) -> bool:
        q = select(model.id).where(model.org_id == self.org_id, func.lower(model.name) == name.lower())
        if exclude:
            q = q.where(model.id != exclude)
        return (await self.db.scalar(q)) is not None

    # ── channels ───────────────────────────────────────────────────────────────────────────────────────────────────────────
    async def list_channels(self) -> list[AlertChannel]:
        return list((await self.db.scalars(select(AlertChannel).where(AlertChannel.org_id == self.org_id).order_by(AlertChannel.created_at, AlertChannel.name))).all())

    async def get_channel(self, channel_id: uuid.UUID) -> AlertChannel:
        ch = await self.db.get(AlertChannel, channel_id)
        if ch is None or ch.org_id != self.org_id:
            raise not_found("alert_channel", str(channel_id))
        return ch

    async def create_channel(self, *, actor: str, webhook_url: str, **f) -> AlertChannel:
        if await self._name_taken(AlertChannel, f["name"]):
            raise conflict(f"A channel named '{f['name']}' already exists.", code="ALERT_CHANNEL_NAME_TAKEN",
                           resource="alert_channel", field="name")
        await self._check_url(f["kind"], webhook_url)
        channel_id = uuid.uuid4()
        ch = AlertChannel(id=channel_id, org_id=self.org_id, secret_ref=secret_path(channel_id), url_hint=webhooks.hint_of(webhook_url),
                          created_by=actor, **f)
        self.db.add(ch)
        await flush_or_conflict(self.db, f"A channel named '{f['name']}' already exists.", constraint="uq_alert_channel_org_name")
        await get_secrets_backend().put(ch.secret_ref, {"url": webhook_url})
        return ch

    async def update_channel(self, channel_id: uuid.UUID, changes: dict) -> AlertChannel:
        ch = await self.get_channel(channel_id)
        if "name" in changes and changes["name"] != ch.name and await self._name_taken(AlertChannel, changes["name"], exclude=ch.id):
            raise conflict(f"A channel named '{changes['name']}' already exists.", code="ALERT_CHANNEL_NAME_TAKEN",
                           resource="alert_channel", field="name")
        url = (changes.pop("webhook_url", None) or "").strip()
        kind = changes.get("kind", ch.kind)
        if url:
            await self._check_url(kind, url)
        elif kind != ch.kind:
            # The stored URL belongs to the old kind: it has to be replaced together with it.
            raise validation_failed("webhook_url", "Changing the channel type needs the new webhook URL.")
        for k, v in changes.items():
            setattr(ch, k, v)
        if url:
            ch.url_hint = webhooks.hint_of(url)
            await get_secrets_backend().put(ch.secret_ref, {"url": url})
        await flush_or_conflict(self.db, "A channel with this name already exists.", constraint="uq_alert_channel_org_name")
        await self.db.refresh(ch)
        return ch

    async def delete_channel(self, channel_id: uuid.UUID) -> None:
        ch = await self.get_channel(channel_id)
        ref = ch.secret_ref
        await self.db.delete(ch)
        await self.db.flush()
        try:
            await get_secrets_backend().delete(ref)
        except Exception as e:  # noqa: BLE001 - the channel is gone; an orphaned secret is logged, not fatal
            log.warning("alert_channel_secret_delete_failed", channel_id=str(channel_id), error=type(e).__name__)

    async def _check_url(self, kind: str, url: str) -> None:
        try:
            await webhooks.check_webhook_url(kind, url)
        except webhooks.WebhookRejected as e:
            raise validation_failed("webhook_url", str(e)) from None

    async def _url_of(self, ch: AlertChannel) -> str | None:
        try:
            data = await get_secrets_backend().get(ch.secret_ref) or {}
        except Exception as e:  # noqa: BLE001
            log.error("alert_channel_secret_read_failed", channel_id=str(ch.id), error=type(e).__name__)
            return None
        return data.get("url")

    async def _send(self, ch: AlertChannel, message: webhooks.Message, *, client=None) -> webhooks.SendResult:
        url = await self._url_of(ch)
        result = webhooks.SendResult(False, "the webhook URL is missing: edit the channel and enter it again") if not url else \
            await webhooks.send(ch.kind, url, message, client=client)
        ch.last_attempt_at, ch.last_ok, ch.last_detail = now_utc(), result.ok, result.detail[:500]
        return result

    async def test_channel(self, channel_id: uuid.UUID, *, actor: str, client=None) -> tuple[bool, str, AlertChannel]:
        ch = await self.get_channel(channel_id)
        result = await self._send(ch, webhooks.Message(
            title="Test alert from InAiEra Workspace", severity="low",
            summary="This is a test message. If you can read it, this channel is set up correctly and real alerts will arrive here.",
            facts=(("Channel", ch.name), ("Sent by", actor), ("Receives", f"{ch.min_severity} and above")),
        ), client=client)
        await self.db.flush()
        await self.db.refresh(ch)
        return result.ok, result.detail, ch

    # ── alerts ─────────────────────────────────────────────────────────────────────────────────────────────────────────────
    def _tab_filter(self, state: str):
        # Open = sent to a channel and not yet acknowledged; acknowledged / resolved list as they are.
        if state == "open":
            return (Alert.state == "open", Alert.delivery == "sent")
        return (Alert.state == state,)

    async def list_alerts(self, state: str, *, start: datetime | None, end: datetime | None, limit: int, offset: int) -> tuple[list[Alert], int]:
        cond = [Alert.org_id == self.org_id, *self._tab_filter(state)]
        if start:
            cond.append(Alert.raised_at >= start)
        if end:
            cond.append(Alert.raised_at < end)
        total = await self.db.scalar(select(func.count()).select_from(Alert).where(*cond)) or 0
        rows = await self.db.scalars(select(Alert).options(selectinload(Alert.events)).where(*cond)
                                     .order_by(Alert.raised_at.desc(), Alert.id).limit(limit).offset(offset))
        alerts = list(rows.all())
        return alerts, total

    async def counts(self) -> dict[str, int]:
        def one(*cond):
            return select(func.count()).select_from(Alert).where(Alert.org_id == self.org_id, *cond)
        return {
            "open": await self.db.scalar(one(*self._tab_filter("open"))) or 0,
            "acknowledged": await self.db.scalar(one(Alert.state == "acknowledged")) or 0,
            "resolved": await self.db.scalar(one(Alert.state == "resolved")) or 0,
            "undelivered": await self.db.scalar(one(Alert.state == "open", Alert.delivery != "sent")) or 0,
        }

    async def get_alert(self, alert_id: uuid.UUID) -> Alert:
        a = await self.db.get(Alert, alert_id)
        if a is None or a.org_id != self.org_id:
            raise not_found("alert", str(alert_id))
        await self.db.refresh(a, ["events"])
        return a

    async def _move(self, a: Alert, to: str, *, from_states: tuple[str, ...], **values) -> bool:
        """Change the state ONLY IF it is still one of `from_states`, in the database, in one statement. Two clicks at once (a double click
        on a confirm button, the same link opened twice) race: both read "open"; this lets exactly one of them win, so the history gets one
        entry and the channel one follow-up, not two. Returns whether THIS call made the change."""
        res = await self.db.execute(update(Alert).where(Alert.id == a.id, Alert.state.in_(from_states)).values(state=to, **values))
        moved = (getattr(res, "rowcount", 0) or 0) == 1
        await self.db.refresh(a)
        return moved

    async def acknowledge(self, alert_id: uuid.UUID, *, actor: str, note: str | None, client=None) -> Alert:
        a = await self.get_alert(alert_id)
        if a.state == "resolved":
            raise conflict("This alert is already resolved.", code="ALERT_ALREADY_RESOLVED", resource="alert")
        if a.state == "open" and await self._move(a, "acknowledged", from_states=("open",)):
            self.db.add(AlertEvent(alert_id=a.id, action="acknowledged", actor=actor, note=note or None, at=now_utc()))
            await self.db.flush()
            await self.db.refresh(a, ["events"])
            await self.announce(a, "ack", actor, note, client=client)
        return a

    async def resolve(self, alert_id: uuid.UUID, *, actor: str, note: str | None, client=None) -> Alert:
        a = await self.get_alert(alert_id)
        if a.state != "resolved" and await self._move(a, "resolved", from_states=("open", "acknowledged"), resolved_at=now_utc()):
            self.db.add(AlertEvent(alert_id=a.id, action="resolved", actor=actor, note=note or None, at=now_utc()))
            await self.db.flush()
            await self.db.refresh(a, ["events"])
            await self.announce(a, "resolved", actor, note, client=client)
        return a

    async def announce(self, a: Alert, tone: str, actor: str, note: str | None, *, client=None) -> None:
        """Tell the channels that received this alert what happened to it ("acknowledged by ...", "resolved", "cleared automatically"). A card
        posted through a webhook cannot be edited, so this short follow-up is how the channel learns the alert is no longer open.

        Best effort: a channel that cannot be reached never fails the acknowledge / resolve (it is noted in the alert's history). Only
        channels that RECEIVED the alert (its "sent to ..." history) are told, so a channel added later or filtered out hears nothing."""
        if not get_settings().alerts_status_updates:
            return
        received = {e.action[len("sent to "):] for e in a.events if e.action.startswith("sent to ")}
        if not received:
            return
        verb = {"ack": "acknowledged", "resolved": "resolved"}[tone]
        by = actor if actor != SWEEP_ACTOR else "the rule sweep (the failure stopped)"
        label = "cleared automatically" if (tone == "resolved" and actor == SWEEP_ACTOR) else verb
        facts = (("Job", a.job_name), ("Rule", a.rule_name), ("State", label), ("By", by))
        summary = f"{a.rule_name} on {a.job_name} was {label}" + ("." if actor == SWEEP_ACTOR else f" by {actor}.")
        if note and not note.startswith("via the button"):
            summary += f" Note: {note}"
        message = webhooks.Message(title=f"{a.rule_name}: {label}", severity=a.severity, summary=summary, facts=facts,
                                   heading=f"{a.rule_name}: {label}", tone=tone)
        for ch in await self.list_channels():
            if ch.name not in received or not ch.enabled:
                continue
            if tone == "ack":                                   # acknowledged: what is left is resolving it once it is fixed
                message = dataclasses.replace(message, buttons=self._buttons(a, ch, acknowledge=False))
            result = await self._send(ch, message, client=client)
            if not result.ok:
                self.db.add(AlertEvent(alert_id=a.id, action=f"status update to {ch.name} failed", actor=SYSTEM_ACTOR, note=result.detail[:500], at=now_utc()))
        await self.db.flush()

    @staticmethod
    def last_event(a: Alert) -> tuple[str, str, datetime]:
        ev = a.events[-1] if a.events else None
        return (ev.action, ev.actor, aware(ev.at)) if ev else ("raised", SWEEP_ACTOR, aware(a.raised_at))

    # ── what is breaching right now (no side effects) ─────────────────────────────────────────────────────────────────────────
    async def breaching(self) -> list[tuple[Breach, str]]:
        await self.ensure_defaults()
        rules = [_spec(r) for r in await self.list_rules()]
        breaches = evaluate(rules, await self._observations(now_utc()), now_utc())
        live = {a for a in (await self.db.scalars(select(Alert.fingerprint).where(
            Alert.org_id == self.org_id, Alert.state.in_(("open", "acknowledged"))))).all()}
        return [(b, "already_open" if b.fingerprint in live else "would_raise") for b in breaches]

    async def _observations(self, now: datetime) -> list[Obs]:
        rows = await self.db.scalars(select(AlertObservation).where(
            AlertObservation.org_id == self.org_id, AlertObservation.occurred_at >= now - timedelta(seconds=LOOKBACK_SECONDS)))
        return [Obs(r.job_key, r.job_name, r.job_kind, r.category, r.outcome, aware(r.occurred_at), r.ref_id, r.failure_class, r.detail)
                for r in rows.all()]

    # ── the sweep ────────────────────────────────────────────────────────────────────────────────────────────────────────────
    async def take_lease(self, now: datetime) -> bool:
        await self._state()
        res = await self.db.execute(update(AlertState).where(
            AlertState.org_id == self.org_id,
            (AlertState.leased_until.is_(None)) | (AlertState.leased_until < now),
        ).values(leased_until=now + timedelta(seconds=LEASE_SECONDS)))
        await self.db.commit()
        return (getattr(res, "rowcount", 0) or 0) > 0

    async def release_lease(self, now: datetime, ok: bool, detail: str) -> None:
        await self.db.execute(update(AlertState).where(AlertState.org_id == self.org_id).values(
            leased_until=None, last_sweep_at=now, last_sweep_ok=ok, last_sweep_detail=detail[:1000]))
        await self.db.commit()

    async def sweep(self, *, client=None, collect: bool = True) -> SweepSummary:
        """One full sweep (see the module docstring). Never raises for a source being down; commits as it goes."""
        now = now_utc()
        if not await self.take_lease(now):
            return SweepSummary(False, "A sweep is already running.")
        summary = SweepSummary(True)
        ok, problems = True, []
        try:
            await self.ensure_defaults()
            await self.db.commit()
            if collect:
                problems = await self._collect(now, summary)
            await self._evaluate_and_apply(now, summary, client=client)
            await self._deliver(now, summary, client=client)
            await self._prune(now)
            await self.db.commit()
        except Exception as e:  # noqa: BLE001 - a broken sweep must not kill the loop; it is reported on the next read
            await self.db.rollback()
            ok = False
            problems.append(f"sweep failed: {type(e).__name__}")
            log.exception("alerts.sweep_failed")
        summary.detail = "; ".join(problems) if problems else "ok"
        await self.release_lease(now, ok and not problems, summary.detail)
        return summary

    async def _collect(self, now: datetime, summary: SweepSummary) -> list[str]:
        known = set((await self.db.scalars(select(AlertObservation.ref_id).where(AlertObservation.org_id == self.org_id))).all())
        runner, names = await sources.collect_runner(known)
        flink = await sources.collect_flink()
        dagster = await sources.collect_dagster(known, names)
        problems: list[str] = []
        fresh: list[Obs] = []
        for r in (runner, flink, dagster):
            summary.sources.append(r.source if r.ok else f"{r.source} (not read)")
            if not r.ok:
                problems.append(r.detail)
            fresh.extend(r.observations)
        seen: set[tuple[str, str]] = set()
        for o in fresh:
            source = {"run": "dbt-runner", "flink_state": "flink"}.get(o.category, "dagster")
            if (source, o.ref_id) in seen or o.ref_id in known:
                continue
            seen.add((source, o.ref_id))
            self.db.add(AlertObservation(
                org_id=self.org_id, source=source, ref_id=o.ref_id, job_key=o.job_key, job_name=o.job_name, job_kind=o.job_kind,
                category=o.category, outcome=o.outcome, failure_class=o.failure_class, detail=(o.detail or None) and o.detail[:2000],
                occurred_at=o.occurred_at))
        await self.db.commit()
        return problems

    async def _evaluate_and_apply(self, now: datetime, summary: SweepSummary, *, client=None) -> None:
        rules = list((await self.db.scalars(select(AlertRule).where(AlertRule.org_id == self.org_id))).all())
        specs = [_spec(r) for r in rules]
        breaches = evaluate(specs, await self._observations(now), now)
        live_rows = list((await self.db.scalars(select(Alert).where(
            Alert.org_id == self.org_id, Alert.state.in_(("open", "acknowledged"))))).all())
        live = [LiveAlert(str(a.id), a.fingerprint, a.state, str(a.rule_id) if a.rule_id else None) for a in live_rows]
        plan = reconcile(breaches, live, evaluated_rule_ids=[s.id for s in specs if s.enabled])
        by_id = {str(a.id): a for a in live_rows}
        cleared: list[Alert] = []
        rule_by_id = {str(r.id): r for r in rules}
        for b in plan.raise_new:
            a = Alert(org_id=self.org_id, rule_id=uuid.UUID(b.rule_id), rule_name=b.rule_name, kind=b.kind, severity=b.severity,
                      job_key=b.job_key, job_name=b.job_name, job_kind=b.job_kind, summary=b.summary, fingerprint=b.fingerprint,
                      state="open", delivery="pending", raised_at=now, last_run_id=b.last_run_id)
            a.events.append(AlertEvent(action="raised", actor=SWEEP_ACTOR, at=now))
            self.db.add(a)
            summary.raised += 1
        for existing, b in plan.keep:
            a = by_id[existing.id]
            a.summary, a.last_run_id = b.summary, b.last_run_id
            if b.severity != a.severity and b.rule_id in rule_by_id:
                a.severity = b.severity
        for existing in plan.clear:
            a = by_id[existing.id]
            a.state, a.resolved_at = "resolved", now
            self.db.add(AlertEvent(alert_id=a.id, action="cleared automatically", actor=SWEEP_ACTOR, at=now))
            summary.cleared += 1
            cleared.append(a)
        await self.db.commit()
        for a in cleared:                                      # tell the channels (after the commit: a slow webhook must not hold the sweep's changes)
            await self.db.refresh(a, ["events"])
            await self.announce(a, "resolved", SWEEP_ACTOR, None, client=client)
        await self.db.commit()

    def _matching(self, alert: Alert, channels: Iterable[AlertChannel]) -> list[AlertChannel]:
        return [c for c in channels if c.enabled and SEVERITY_RANK[alert.severity] >= SEVERITY_RANK[c.min_severity]
                and (c.scope_job_key is None or c.scope_job_key == alert.job_key)]

    async def _deliver(self, now: datetime, summary: SweepSummary, *, client=None) -> None:
        max_attempts = get_settings().alerts_delivery_max_attempts
        channels = await self.list_channels()
        pending = list((await self.db.scalars(select(Alert).where(
            Alert.org_id == self.org_id, Alert.state != "resolved", Alert.delivery != "sent").order_by(Alert.raised_at))).all())
        for a in pending:
            matching = self._matching(a, channels)
            if not matching:
                if a.delivery != "no_channel":
                    a.delivery = "no_channel"
                continue
            if a.delivery == "failed" and a.delivery_attempts >= max_attempts:
                continue
            facts = (("Severity", a.severity.upper()), ("Job", a.job_name), ("Kind", JOB_KIND_LABEL.get(a.job_kind, a.job_kind)),
                     ("State", a.state), ("Raised", aware(a.raised_at).strftime("%Y-%m-%d %H:%M UTC")))
            any_ok = False
            for ch in matching:
                message = webhooks.Message(
                    title=f"{JOB_KIND_LABEL.get(a.job_kind, 'Job')} '{a.job_name}': {a.rule_name}", severity=a.severity, summary=a.summary,
                    facts=facts, heading=a.rule_name, buttons=self._buttons(a, ch))
                result = await self._send(ch, message, client=client)
                any_ok = any_ok or result.ok
                self.db.add(AlertEvent(alert_id=a.id, action=f"sent to {ch.name}" if result.ok else f"delivery to {ch.name} failed",
                                       actor=SYSTEM_ACTOR, note=None if result.ok else result.detail[:500], at=now_utc()))
            a.delivery_attempts += 1
            if any_ok:
                a.delivery, a.sent_at = "sent", now_utc()
                summary.delivered += 1
            else:
                a.delivery = "failed"
        await self.db.commit()

    @staticmethod
    def _buttons(alert: Alert, ch: AlertChannel, *, acknowledge: bool = True) -> tuple[webhooks.Button, ...]:
        """Acknowledge / Resolve as links in the message, when links are set up (ALERTS_PUBLIC_URL + ALERTS_ACTION_SECRET). Each link is
        signed for THIS alert and THIS channel, so the action is recorded as done by that channel. `acknowledge=False`: only Resolve (the
        follow-up to an acknowledge: the next step is resolving it once it is fixed)."""
        if not links.enabled():
            return ()
        i = str(alert.id)
        resolve = webhooks.Button("Resolve", links.action_url(i, "resolve", ch.kind, ch.name), "positive")
        return (webhooks.Button("Acknowledge", links.action_url(i, "acknowledge", ch.kind, ch.name)), resolve) if acknowledge else (resolve,)

    async def _prune(self, now: datetime) -> None:
        cutoff = now - timedelta(days=get_settings().alerts_retention_days)
        await self.db.execute(delete(AlertObservation).where(AlertObservation.org_id == self.org_id, AlertObservation.occurred_at < cutoff))
