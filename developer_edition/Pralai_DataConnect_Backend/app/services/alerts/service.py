"""The database side of alerts for ONE organization: rules and channels (CRUD), the alert lifecycle (acknowledge / resolve) and the sweep.

One sweep, every minute (sweeper.py) or on demand ("Run rules now"):

  1. take the organization's sweep lease (one sweep at a time across replicas)   6. raise the new breaches, keep the reported ones, clear
  2. create the default rules, once                                                 the ones that stopped (engine.reconcile)
  3. read what happened (sources.py), each source on its own                     7. deliver every alert not yet sent to the channels that
  4. remember the state changes / operations / pauses (dc_alert_observations)       match it (retrying the failed ones, a few times)
  5. evaluate the enabled rules over those plus the live counters                 8. forget old observations; release the lease

An alert is raised once per breach and stays quiet while it is open or acknowledged; it is cleared automatically when its condition stops.
After a person resolves it by hand, the same rule raises again for that pipeline only on newer evidence (engine.reconcile).

Ported from the Workspace's AlertService (Pralai_workspace_backend/app/services/alerts/service.py); the traps it documents are kept:
atomic state changes, commits before any network call, follow-ups only to channels that received the alert.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import delete, exists, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, selectinload

from app.core.config import settings
from app.core.errors import DataConnectError, ValidationError
from app.models import (
    Alert,
    AlertChannel,
    AlertChannelSecret,
    AlertEvent,
    AlertObservation,
    AlertRule,
    AlertState,
)
from app.schemas.errors import FieldError
from app.services import secret_crypto
from app.services.alerts import links, sources, webhooks
from app.services.alerts.engine import Breach, LiveAlert, Obs, RuleSpec, evaluate, reconcile
from app.services.alerts.kinds import (
    DEFAULT_RULES,
    DIRECTION_LABEL,
    LOOKBACK_SECONDS,
    RULE_KINDS,
    SEVERITY_RANK,
    WINDOWS,
)

log = structlog.get_logger(__name__)

SWEEP_ACTOR = "rule sweep"
SYSTEM_ACTOR = "system"
LEASE_SECONDS = 120
STORED_SOURCES = {"state": "runtime", "operation": "operation", "threshold": "audit"}


def now_utc() -> datetime:
    return datetime.now(UTC)


def aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


# ── errors (DataConnect's envelope: app/core/errors.py) ─────────────────────────────────────────────────────────────────────
class AlertError(DataConnectError):
    """A specific, UI-safe alerts error with its own stable code (ALERT_RULE_NAME_TAKEN, ALERT_ALREADY_RESOLVED, ...)."""

    def __init__(self, message: str, *, code: str, status_code: int, field: str | None = None) -> None:
        super().__init__(message, status_code=status_code,
                         field_errors=[FieldError(field=field, message=message, code=code)] if field else None)
        self.code = code


def conflict(message: str, *, code: str, field: str | None = None) -> AlertError:
    return AlertError(message, code=code, status_code=409, field=field)


def not_found(what: str) -> AlertError:
    return AlertError(f"That {what} does not exist.", code="not_found", status_code=404)


def invalid(field_name: str, message: str) -> ValidationError:
    return ValidationError(message, field_errors=[FieldError(field=field_name, message=message)])


# ── validation ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────
def validate_rule_numbers(kind: str, threshold: int, window_seconds: int, min_runs: int) -> None:
    info = RULE_KINDS[kind]
    if threshold > info.max_threshold:
        raise invalid("threshold", f"The threshold of a '{info.label}' rule can be at most {info.max_threshold:,}.")
    if window_seconds not in WINDOWS:
        raise invalid("window_seconds", "Pick one of the offered windows.")
    if not info.uses_window and window_seconds:
        raise invalid("window_seconds", f"A '{info.label}' rule has no window: it fires while the pipeline is in that state.")
    if not info.uses_min_runs and min_runs != 1:
        raise invalid("min_runs", f"A '{info.label}' rule has no minimum number of runs.")


def _spec(r: AlertRule) -> RuleSpec:
    return RuleSpec(str(r.id), r.name, r.kind, r.severity, r.threshold, r.window_seconds, r.min_runs, r.scope_pipeline_id, r.enabled)


@dataclass
class SweepSummary:
    ran: bool
    detail: str = ""
    raised: int = 0
    cleared: int = 0
    delivered: int = 0
    sources: list[str] = field(default_factory=list)


class AlertService:
    def __init__(self, db: AsyncSession, org_id: str):
        self.db = db
        self.org_id = org_id

    # ── rules ──────────────────────────────────────────────────────────────────────────────────────────────────────────────
    async def ensure_defaults(self) -> None:
        """The rules every organization starts with, created once (an admin may change or delete them; they do not come back)."""
        state = await self._state()
        if state.defaults_seeded:
            return
        existing = {n.lower() for n in (await self.db.scalars(select(AlertRule.name).where(AlertRule.org_id == self.org_id))).all()}
        for d in DEFAULT_RULES:
            if d.name.lower() not in existing:
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
        return list((await self.db.scalars(select(AlertRule).where(AlertRule.org_id == self.org_id)
                                           .order_by(AlertRule.created_at, AlertRule.name))).all())

    async def get_rule(self, rule_id: str) -> AlertRule:
        rule = await self.db.get(AlertRule, _uuid(rule_id))
        if rule is None or rule.org_id != self.org_id:
            raise not_found("alert rule")
        return rule

    async def create_rule(self, *, actor: str, **f) -> AlertRule:
        validate_rule_numbers(f["kind"], f["threshold"], f["window_seconds"], f["min_runs"])
        await self.ensure_defaults()
        if await self._name_taken(AlertRule, f["name"]):
            raise conflict(f"A rule named '{f['name']}' already exists.", code="ALERT_RULE_NAME_TAKEN", field="name")
        rule = AlertRule(org_id=self.org_id, created_by=actor, **f)
        self.db.add(rule)
        await self._flush_or_conflict(f"A rule named '{f['name']}' already exists.", "ALERT_RULE_NAME_TAKEN")
        await self.db.refresh(rule)
        return rule

    async def update_rule(self, rule_id: str, changes: dict) -> AlertRule:
        rule = await self.get_rule(rule_id)
        if "name" in changes and changes["name"] != rule.name and await self._name_taken(AlertRule, changes["name"], exclude=rule.id):
            raise conflict(f"A rule named '{changes['name']}' already exists.", code="ALERT_RULE_NAME_TAKEN", field="name")
        merged = {k: changes.get(k, getattr(rule, k)) for k in ("kind", "threshold", "window_seconds", "min_runs")}
        # Changing to a kind without a window / minimum resets the fields it does not use rather than failing on stale values.
        info = RULE_KINDS[merged["kind"]]
        if not info.uses_window and "window_seconds" not in changes:
            merged["window_seconds"] = 0
        if not info.uses_min_runs and "min_runs" not in changes:
            merged["min_runs"] = 1
        if merged["threshold"] > info.max_threshold and "threshold" not in changes:
            merged["threshold"] = info.max_threshold
        validate_rule_numbers(merged["kind"], merged["threshold"], merged["window_seconds"], merged["min_runs"])
        for k, v in {**changes, **merged}.items():
            setattr(rule, k, v)
        await self._flush_or_conflict("A rule with this name already exists.", "ALERT_RULE_NAME_TAKEN")
        await self.db.refresh(rule)                       # updated_at is set by the database on UPDATE
        return rule

    async def delete_rule(self, rule_id: str) -> None:
        await self.db.delete(await self.get_rule(rule_id))
        await self.db.flush()

    async def _name_taken(self, model, name: str, exclude: str | None = None) -> bool:
        q = select(model.id).where(model.org_id == self.org_id, func.lower(model.name) == name.lower())
        if exclude:
            q = q.where(model.id != exclude)
        return (await self.db.scalar(q)) is not None

    async def _flush_or_conflict(self, message: str, code: str) -> None:
        """Two requests creating the same name at once both pass _name_taken; the unique index refuses the second: a 409, not a 500."""
        try:
            await self.db.flush()
        except IntegrityError as e:
            await self.db.rollback()
            if "uq_dc_alert" in str(e.orig):
                raise conflict(message, code=code, field="name") from None
            raise

    # ── channels ───────────────────────────────────────────────────────────────────────────────────────────────────────────
    async def list_channels(self) -> list[AlertChannel]:
        return list((await self.db.scalars(select(AlertChannel).where(AlertChannel.org_id == self.org_id)
                                           .order_by(AlertChannel.created_at, AlertChannel.name))).all())

    async def get_channel(self, channel_id: str) -> AlertChannel:
        ch = await self.db.get(AlertChannel, _uuid(channel_id))
        if ch is None or ch.org_id != self.org_id:
            raise not_found("channel")
        return ch

    async def create_channel(self, *, actor: str, webhook_url: str, **f) -> AlertChannel:
        if await self._name_taken(AlertChannel, f["name"]):
            raise conflict(f"A channel named '{f['name']}' already exists.", code="ALERT_CHANNEL_NAME_TAKEN", field="name")
        await self._check_url(f["kind"], webhook_url)
        channel_id = str(uuid.uuid4())
        ch = AlertChannel(id=channel_id, org_id=self.org_id, url_hint=webhooks.hint_of(webhook_url), created_by=actor, **f)
        self.db.add(ch)
        await self._flush_or_conflict(f"A channel named '{f['name']}' already exists.", "ALERT_CHANNEL_NAME_TAKEN")
        self.db.add(AlertChannelSecret(channel_id=channel_id, envelope=self._encrypt(channel_id, webhook_url)))
        await self.db.flush()
        await self.db.refresh(ch)
        return ch

    async def update_channel(self, channel_id: str, changes: dict) -> AlertChannel:
        ch = await self.get_channel(channel_id)
        if "name" in changes and changes["name"] != ch.name and await self._name_taken(AlertChannel, changes["name"], exclude=ch.id):
            raise conflict(f"A channel named '{changes['name']}' already exists.", code="ALERT_CHANNEL_NAME_TAKEN", field="name")
        url = (changes.pop("webhook_url", None) or "").strip()
        kind = changes.get("kind", ch.kind)
        if url:
            await self._check_url(kind, url)
        elif kind != ch.kind:
            # The stored URL belongs to the old kind: it has to be replaced together with it.
            raise invalid("webhook_url", "Changing the channel type needs the new webhook URL.")
        for k, v in changes.items():
            setattr(ch, k, v)
        if url:
            ch.url_hint = webhooks.hint_of(url)
            secret = await self.db.get(AlertChannelSecret, ch.id)
            envelope = self._encrypt(str(ch.id), url)
            if secret is None:
                self.db.add(AlertChannelSecret(channel_id=str(ch.id), envelope=envelope))
            else:
                secret.envelope = envelope
        await self._flush_or_conflict("A channel with this name already exists.", "ALERT_CHANNEL_NAME_TAKEN")
        await self.db.refresh(ch)
        return ch

    async def delete_channel(self, channel_id: str) -> None:
        await self.db.delete(await self.get_channel(channel_id))     # its encrypted URL goes with it (ON DELETE CASCADE)
        await self.db.flush()

    async def _check_url(self, kind: str, url: str) -> None:
        try:
            await webhooks.check_webhook_url(kind, url, org_id=self.org_id)
        except webhooks.WebhookRejected as e:
            raise invalid("webhook_url", str(e)) from None

    def _encrypt(self, channel_id: str, url: str) -> dict:
        # The channel id is the additional authenticated data: an envelope copied onto another channel does not decrypt.
        return secret_crypto.encrypt_fields({"url": url}, org_id=self.org_id, connection_id=channel_id)

    async def _url_of(self, ch: AlertChannel) -> str | None:
        secret = await self.db.get(AlertChannelSecret, ch.id)
        if secret is None:
            return None
        try:
            return secret_crypto.decrypt_fields(secret.envelope, org_id=self.org_id, connection_id=str(ch.id)).get("url")
        except Exception as e:  # noqa: BLE001 - e.g. the key it was encrypted with is no longer configured
            log.error("alerts.channel_secret_unreadable", channel_id=str(ch.id), error=type(e).__name__)
            return None

    async def _send(self, ch: AlertChannel, message: webhooks.Message, *, client=None) -> webhooks.SendResult:
        url = await self._url_of(ch)
        result = webhooks.SendResult(False, "the webhook URL is missing: edit the channel and enter it again") if not url else \
            await webhooks.send(ch.kind, url, message, client=client, org_id=self.org_id)
        ch.last_attempt_at, ch.last_ok, ch.last_detail = now_utc(), result.ok, result.detail[:500]
        return result

    async def test_channel(self, channel_id: str, *, actor: str, client=None) -> tuple[bool, str, AlertChannel]:
        ch = await self.get_channel(channel_id)
        result = await self._send(ch, webhooks.Message(
            title="Test alert from InAiEra DataConnect", severity="low",
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
        return list(rows.all()), total

    async def counts(self) -> dict[str, int]:
        def one(*cond):
            return select(func.count()).select_from(Alert).where(Alert.org_id == self.org_id, *cond)
        return {
            "open": await self.db.scalar(one(*self._tab_filter("open"))) or 0,
            "acknowledged": await self.db.scalar(one(Alert.state == "acknowledged")) or 0,
            "resolved": await self.db.scalar(one(Alert.state == "resolved")) or 0,
            "undelivered": await self.db.scalar(one(Alert.state == "open", Alert.delivery != "sent")) or 0,
        }

    async def get_alert(self, alert_id: str) -> Alert:
        a = await self.db.get(Alert, _uuid(alert_id))
        if a is None or a.org_id != self.org_id:
            raise not_found("alert")
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

    async def acknowledge(self, alert_id: str, *, actor: str, note: str | None, client=None) -> Alert:
        a = await self.get_alert(alert_id)
        if a.state == "resolved":
            raise conflict("This alert is already resolved.", code="ALERT_ALREADY_RESOLVED")
        if a.state == "open" and await self._move(a, "acknowledged", from_states=("open",)):
            self.db.add(AlertEvent(alert_id=a.id, action="acknowledged", actor=actor, note=note or None, at=now_utc()))
            await self.db.flush()
            await self.db.refresh(a, ["events"])
            await self.announce(a, "ack", actor, note, client=client)
        return a

    async def resolve(self, alert_id: str, *, actor: str, note: str | None, client=None) -> Alert:
        a = await self.get_alert(alert_id)
        if a.state != "resolved" and await self._move(a, "resolved", from_states=("open", "acknowledged"), resolved_at=now_utc()):
            self.db.add(AlertEvent(alert_id=a.id, action="resolved", actor=actor, note=note or None, at=now_utc()))
            await self.db.flush()
            await self.db.refresh(a, ["events"])
            await self.announce(a, "resolved", actor, note, client=client)
        return a

    async def announce(self, a: Alert, tone: str, actor: str, note: str | None, *, client=None) -> None:
        """Tell the channels that received this alert what happened to it ("acknowledged by ...", "resolved", "cleared automatically"). A
        card posted through a webhook cannot be edited, so this short follow-up is how the channel learns the alert is no longer open.

        Best effort: a channel that cannot be reached never fails the acknowledge / resolve (it is noted in the alert's history). Only
        channels that RECEIVED the alert (its "sent to ..." history) are told, so a channel added later or filtered out hears nothing."""
        if not settings.alerts_status_updates:
            return
        received = {e.action[len("sent to "):] for e in a.events if e.action.startswith("sent to ")}
        if not received:
            return
        verb = {"ack": "acknowledged", "resolved": "resolved"}[tone]
        by = actor if actor != SWEEP_ACTOR else "the rule sweep (the condition stopped)"
        label = "cleared automatically" if (tone == "resolved" and actor == SWEEP_ACTOR) else verb
        facts = (("Pipeline", a.pipeline_name), ("Rule", a.rule_name), ("State", label), ("By", by))
        summary = f"{a.rule_name} on {a.pipeline_name} was {label}" + ("." if actor == SWEEP_ACTOR else f" by {actor}.")
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
                self.db.add(AlertEvent(alert_id=a.id, action=f"status update to {ch.name} failed", actor=SYSTEM_ACTOR,
                                       note=result.detail[:500], at=now_utc()))
        await self.db.flush()

    @staticmethod
    def last_event(a: Alert) -> tuple[str, str, datetime]:
        ev = a.events[-1] if a.events else None
        return (ev.action, ev.actor, aware(ev.at)) if ev else ("raised", SWEEP_ACTOR, aware(a.raised_at))

    # ── the sweep ──────────────────────────────────────────────────────────────────────────────────────────────────────────
    async def take_lease(self, now: datetime) -> bool:
        await self._state()
        await self.db.commit()
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

    async def sweep(self, *, client=None) -> SweepSummary:
        """One full sweep (see the module docstring). Never raises for a source being unreadable; commits as it goes."""
        now = now_utc()
        if not await self.take_lease(now):
            return SweepSummary(False, "A sweep is already running.")
        summary = SweepSummary(True)
        ok, problems = True, []
        try:
            await self.ensure_defaults()
            await self.db.commit()
            live, problems = await self._collect(now, summary)
            await self._evaluate_and_apply(now, live, summary, client=client)
            await self._deliver(now, summary, client=client)
            await self._prune(now)
            await self.db.commit()
        except Exception as e:  # noqa: BLE001 - a broken sweep must not kill the loop; it is reported on the next read
            await self.db.rollback()
            ok = False
            problems.append(f"sweep failed: {type(e).__name__}")
            log.exception("alerts.sweep_failed", org_id=self.org_id)
        summary.detail = "; ".join(problems) if problems else "ok"
        await self.release_lease(now, ok and not problems, summary.detail)
        return summary

    async def _collect(self, now: datetime, summary: SweepSummary) -> tuple[list[Obs], list[str]]:
        """Store the new state changes / operations / pauses; return the live observations (counters, drift) and any source problems."""
        pipelines = await sources.pipelines_of(self.db, self.org_id)
        known = set((await self.db.scalars(select(AlertObservation.ref_id).where(
            AlertObservation.org_id == self.org_id, AlertObservation.source.in_(("operation", "audit"))))).all())
        last = await self._last_states()
        stored = [
            await sources.collect_state(self.db, self.org_id, pipelines, last, now),
            await sources.collect_operations(self.db, self.org_id, pipelines, known, now),
            await sources.collect_threshold_pauses(self.db, self.org_id, pipelines, known, now),
        ]
        live = [await sources.collect_counts(self.db, self.org_id, pipelines, now),
                await sources.collect_drift(self.db, self.org_id, pipelines)]
        problems: list[str] = []
        for r in (*stored, *live):
            summary.sources.append(r.source if r.ok else f"{r.source} (not read)")
            if not r.ok:
                problems.append(r.detail)
        seen: set[tuple[str, str]] = set()
        for r in stored:
            for o in r.observations:
                source = STORED_SOURCES[o.category]
                if (source, o.ref_id) in seen:
                    continue
                seen.add((source, o.ref_id))
                self.db.add(AlertObservation(
                    org_id=self.org_id, source=source, ref_id=o.ref_id[:300], pipeline_id=o.pipeline_id, pipeline_name=o.pipeline_name[:255],
                    direction=o.direction, category=o.category, outcome=o.outcome, detail=(o.detail or None) and o.detail[:2000],
                    occurred_at=o.occurred_at))
        await self.db.commit()
        return [o for r in live for o in r.observations], problems

    async def _last_states(self) -> dict[str, tuple[str, str | None]]:
        """pipeline id -> (outcome, detail) of its newest stored state observation."""
        newest = (select(AlertObservation.pipeline_id, func.max(AlertObservation.occurred_at).label("at"))
                  .where(AlertObservation.org_id == self.org_id, AlertObservation.category == "state")
                  .group_by(AlertObservation.pipeline_id).subquery())
        rows = (await self.db.execute(
            select(AlertObservation.pipeline_id, AlertObservation.outcome, AlertObservation.detail, AlertObservation.created_at)
            .join(newest, (AlertObservation.pipeline_id == newest.c.pipeline_id) & (AlertObservation.occurred_at == newest.c.at))
            .where(AlertObservation.org_id == self.org_id, AlertObservation.category == "state")
            .order_by(AlertObservation.created_at))).all()
        return {r.pipeline_id: (r.outcome, r.detail) for r in rows}

    async def _observations(self, now: datetime) -> list[Obs]:
        rows = await self.db.scalars(select(AlertObservation).where(
            AlertObservation.org_id == self.org_id, AlertObservation.occurred_at >= now - timedelta(seconds=LOOKBACK_SECONDS)))
        out = [Obs(r.pipeline_id, r.pipeline_name, r.direction, r.category, r.outcome, aware(r.occurred_at), r.ref_id, r.detail)
               for r in rows.all()]
        # A pipeline's CURRENT state may have been reached longer ago than the lookback: keep its newest state observation regardless, and its
        # newest KNOWN one ("unknown" readings neither raise nor clear a failed / degraded alert, so the rules look past them).
        seen = {(o.category, o.ref_id) for o in out}
        for only_known in (False, True):
            where = [AlertObservation.org_id == self.org_id, AlertObservation.category == "state"]
            if only_known:
                where.append(AlertObservation.outcome != "unknown")
            newest = (select(AlertObservation.pipeline_id, func.max(AlertObservation.occurred_at).label("at"))
                      .where(*where).group_by(AlertObservation.pipeline_id).subquery())
            older = await self.db.scalars(select(AlertObservation).join(
                newest, (AlertObservation.pipeline_id == newest.c.pipeline_id) & (AlertObservation.occurred_at == newest.c.at)).where(*where))
            for r in older.all():
                if (r.category, r.ref_id) not in seen:
                    seen.add((r.category, r.ref_id))
                    out.append(Obs(r.pipeline_id, r.pipeline_name, r.direction, r.category, r.outcome, aware(r.occurred_at), r.ref_id, r.detail))
        return out

    @staticmethod
    def _current(observations: list[Obs], pipelines: dict[str, sources.PipelineInfo]) -> list[Obs]:
        """Only pipelines that still exist, under their CURRENT name: a deleted pipeline's remembered failure must not keep its alert open (it
        is cleared), and a renamed one is reported by the name people see now."""
        return [dataclasses.replace(o, pipeline_name=pipelines[o.pipeline_id].name, direction=pipelines[o.pipeline_id].direction)
                for o in observations if o.pipeline_id in pipelines]

    async def _resolved_by_hand(self, now: datetime) -> dict[str, datetime]:
        """fingerprint -> when a PERSON last resolved that rule's alert for that pipeline (not the sweep clearing it)."""
        rows = (await self.db.execute(
            select(Alert.fingerprint, func.max(AlertEvent.at))
            .join(AlertEvent, AlertEvent.alert_id == Alert.id)
            .where(Alert.org_id == self.org_id, Alert.state == "resolved", AlertEvent.action == "resolved",
                   AlertEvent.at >= now - timedelta(seconds=LOOKBACK_SECONDS))
            .group_by(Alert.fingerprint))).all()
        return {fp: aware(at) for fp, at in rows}

    async def breaching(self) -> list[tuple[Breach, str]]:
        """What breaches a rule right now (nothing is raised or sent by asking)."""
        now = now_utc()
        rules = [_spec(r) for r in await self.list_rules()]
        pipelines = await sources.pipelines_of(self.db, self.org_id)
        live = [o for r in (await sources.collect_counts(self.db, self.org_id, pipelines, now),
                            await sources.collect_drift(self.db, self.org_id, pipelines)) for o in r.observations]
        breaches = evaluate(rules, self._current([*await self._observations(now), *live], pipelines), now)
        open_fps = set((await self.db.scalars(select(Alert.fingerprint).where(
            Alert.org_id == self.org_id, Alert.state.in_(("open", "acknowledged"))))).all())
        by_hand = await self._resolved_by_hand(now)

        def state(b: Breach) -> str:
            if b.fingerprint in open_fps:
                return "already_open"
            # the sweep does not raise it again: a person resolved it and nothing newer has happened since (engine.reconcile)
            return "resolved_by_hand" if b.fingerprint in by_hand and b.evidence_at <= by_hand[b.fingerprint] else "would_raise"
        return [(b, state(b)) for b in breaches]

    async def _evaluate_and_apply(self, now: datetime, live_obs: list[Obs], summary: SweepSummary, *, client=None) -> None:
        rules = list((await self.db.scalars(select(AlertRule).where(AlertRule.org_id == self.org_id))).all())
        specs = [_spec(r) for r in rules]
        pipelines = await sources.pipelines_of(self.db, self.org_id)
        breaches = evaluate(specs, self._current([*await self._observations(now), *live_obs], pipelines), now)
        live_rows = list((await self.db.scalars(select(Alert).where(
            Alert.org_id == self.org_id, Alert.state.in_(("open", "acknowledged"))))).all())
        live = [LiveAlert(str(a.id), a.fingerprint, a.state, str(a.rule_id) if a.rule_id else None) for a in live_rows]
        plan = reconcile(breaches, live, evaluated_rule_ids=[s.id for s in specs if s.enabled],
                         resolved_by_hand=await self._resolved_by_hand(now))
        by_id = {str(a.id): a for a in live_rows}
        rule_by_id = {str(r.id): r for r in rules}
        cleared: list[Alert] = []
        for b in plan.raise_new:
            a = Alert(org_id=self.org_id, rule_id=b.rule_id, rule_name=b.rule_name, kind=b.kind, severity=b.severity,
                      pipeline_id=b.pipeline_id, pipeline_name=b.pipeline_name[:255], direction=b.direction, summary=b.summary,
                      fingerprint=b.fingerprint, state="open", delivery="pending", delivery_attempts=0, raised_at=now,
                      evidence_at=b.evidence_at, last_ref_id=(b.last_ref_id or None) and b.last_ref_id[:300])
            a.events.append(AlertEvent(action="raised", actor=SWEEP_ACTOR, at=now))
            self.db.add(a)
            summary.raised += 1
        for existing, b in plan.keep:
            a = by_id[existing.id]
            a.summary, a.evidence_at = b.summary, b.evidence_at
            a.last_ref_id = (b.last_ref_id or None) and b.last_ref_id[:300]
            if b.severity != a.severity and b.rule_id in rule_by_id:
                a.severity = b.severity
        for existing in plan.clear:
            a = by_id[existing.id]
            a.state, a.resolved_at = "resolved", now
            self.db.add(AlertEvent(alert_id=a.id, action="cleared automatically", actor=SWEEP_ACTOR, at=now))
            summary.cleared += 1
            cleared.append(a)
        await self.db.commit()
        for a in cleared:                          # tell the channels (after the commit: a slow webhook must not hold the sweep's changes)
            await self.db.refresh(a, ["events"])
            await self.announce(a, "resolved", SWEEP_ACTOR, None, client=client)
        await self.db.commit()

    def _matching(self, alert: Alert, channels: Iterable[AlertChannel]) -> list[AlertChannel]:
        return [c for c in channels if c.enabled and SEVERITY_RANK[alert.severity] >= SEVERITY_RANK[c.min_severity]
                and (c.scope_pipeline_id is None or c.scope_pipeline_id == alert.pipeline_id)]

    async def _deliver(self, now: datetime, summary: SweepSummary, *, client=None) -> None:
        max_attempts = settings.alerts_delivery_max_attempts
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
            facts = (("Severity", a.severity.upper()), ("Pipeline", a.pipeline_name),
                     ("Kind", DIRECTION_LABEL.get(a.direction, a.direction.lower())), ("State", a.state),
                     ("Raised", aware(a.raised_at).strftime("%Y-%m-%d %H:%M UTC")))
            any_ok = False
            for ch in matching:
                message = webhooks.Message(
                    title=f"Pipeline '{a.pipeline_name}': {a.rule_name}", severity=a.severity, summary=a.summary,
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
        """Forget old observations — but never a pipeline's newest state observation, nor its newest known one: they are the states the rules
        still compare against."""
        cutoff = now - timedelta(days=settings.alerts_retention_days)
        newer = aliased(AlertObservation)
        has_newer_state = exists().where(
            newer.org_id == AlertObservation.org_id, newer.pipeline_id == AlertObservation.pipeline_id, newer.category == "state",
            newer.occurred_at > AlertObservation.occurred_at, (AlertObservation.outcome == "unknown") | (newer.outcome != "unknown"))
        await self.db.execute(delete(AlertObservation).where(
            AlertObservation.org_id == self.org_id, AlertObservation.occurred_at < cutoff,
            (AlertObservation.category != "state") | has_newer_state))


def _uuid(value: str) -> str:
    """A path id as the canonical UUID string; anything else is simply not found (not a 500 from the database)."""
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        raise not_found("item") from None
