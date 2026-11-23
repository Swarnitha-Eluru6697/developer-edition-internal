"""Alerts: failures of the Workspace's jobs, the rules that raise them, and the Slack / Teams channels they are posted to.

  GET    /api/v1/alerts?state=open|acknowledged|resolved   a page of alerts (newest first), optionally between two dates
  GET    /api/v1/alerts/counts                             how many per tab (+ open alerts no channel received)
  GET    /api/v1/alerts/{id}                               one alert with its history
  POST   /api/v1/alerts/{id}/acknowledge | /resolve        (workspace:alerts:edit)
  GET    /api/v1/alerts/rules, /rules/kinds, /rules/breaching;   POST /rules, PATCH /rules/{id}, DELETE /rules/{id};   POST /rules/run
  GET    /api/v1/alerts/channels;   POST /channels, PATCH /channels/{id}, DELETE /channels/{id};   POST /channels/{id}/test

Reading needs alerts_read, acknowledging alerts_ack, everything that changes configuration or sends alerts_manage. A channel's webhook
URL is accepted on create / edit and is never returned (only a short hint such as "hooks.slack.com/…/x7Q2").
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta, timezone

from fastapi import APIRouter, Query, status

from app.core.deps import DB
from app.core.rbac import RequireAlertsAck, RequireAlertsManage, RequireAlertsRead
from app.schemas.alerts import (
    AlertAction, AlertCounts, AlertDetail, AlertOut, AlertsPage, Breaching, ChannelCreate, ChannelOut, ChannelPatch, ChannelTestResult,
    RuleCreate, RuleKindInfo, RuleOut, RulePatch, SweepOut,
)
from app.services.alerts.kinds import RULE_KINDS
from app.services.alerts.service import AlertService
from app.services.audit_service import AuditService
from app.services.workspace_service import WorkspaceService

router = APIRouter(prefix="/api/v1/alerts", tags=["alerts"])


def _actor(user) -> str:
    return getattr(user, "username", None) or user.email or user.user_id


async def _audit(db, user, action: str, resource_type: str, resource_id: uuid.UUID, **meta) -> None:
    row = await WorkspaceService(db).ensure_user(user)
    await AuditService(db).record(user_id=row.id, action=action, resource_type=resource_type, resource_id=resource_id, metadata=meta or None)


def _alert_out(a, svc: AlertService, *, detail: bool = False):
    action, actor, at = svc.last_event(a)
    base = dict(
        id=a.id, rule_id=a.rule_id, rule_name=a.rule_name, kind=a.kind, severity=a.severity, job_key=a.job_key, job_name=a.job_name,
        job_kind=a.job_kind, summary=a.summary, state=a.state, delivery=a.delivery, raised_at=a.raised_at, sent_at=a.sent_at,
        resolved_at=a.resolved_at, last_action=action, last_actor=actor, last_at=at)
    if detail:
        return AlertDetail.model_validate({**base, "events": [dict(action=e.action, actor=e.actor, note=e.note, at=e.at) for e in a.events]})
    return AlertOut.model_validate(base)


def _day_start(d: date | None) -> datetime | None:
    return datetime.combine(d, time.min, tzinfo=timezone.utc) if d else None


# ── alerts ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────
@router.get("", response_model=AlertsPage)
async def list_alerts(
    user: RequireAlertsRead, db: DB,
    state: str = Query("open", pattern="^(open|acknowledged|resolved)$"),
    date_from: date | None = Query(None, alias="from"), date_to: date | None = Query(None, alias="to"),
    limit: int = Query(10, ge=1, le=100), offset: int = Query(0, ge=0),
) -> AlertsPage:
    svc = AlertService(db)
    end = _day_start(date_to)
    alerts, total = await svc.list_alerts(state, start=_day_start(date_from), end=end + timedelta(days=1) if end else None,
                                          limit=limit, offset=offset)
    counts = await svc.counts()
    return AlertsPage(items=[_alert_out(a, svc) for a in alerts], total=total, undelivered=counts["undelivered"])


@router.get("/counts", response_model=AlertCounts)
async def alert_counts(user: RequireAlertsRead, db: DB) -> AlertCounts:
    return AlertCounts(**await AlertService(db).counts())


@router.get("/rules/kinds", response_model=list[RuleKindInfo])
async def rule_kinds(user: RequireAlertsRead) -> list[RuleKindInfo]:
    return [RuleKindInfo.model_validate(dict(kind=k, label=i.label, description=i.description, threshold_hint=i.threshold_hint,
                                             uses_window=i.uses_window, uses_min_runs=i.uses_min_runs, max_threshold=i.max_threshold))
            for k, i in RULE_KINDS.items()]


# ── rules ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────
@router.get("/rules", response_model=list[RuleOut])
async def list_rules(user: RequireAlertsRead, db: DB) -> list[RuleOut]:
    return [RuleOut.model_validate(r) for r in await AlertService(db).list_rules()]


@router.get("/rules/breaching", response_model=list[Breaching])
async def breaching(user: RequireAlertsRead, db: DB) -> list[Breaching]:
    """What is breaching a rule right now (nothing is raised or sent by asking)."""
    return [Breaching.model_validate(dict(rule_id=uuid.UUID(b.rule_id), rule_name=b.rule_name, kind=b.kind, severity=b.severity,
                                          job_key=b.job_key, job_name=b.job_name, job_kind=b.job_kind, summary=b.summary, state=state))
            for b, state in await AlertService(db).breaching()]


@router.post("/rules/run", response_model=SweepOut)
async def run_rules_now(user: RequireAlertsManage, db: DB) -> SweepOut:
    """Run one sweep now ("Run rules now"): read the runners, evaluate every rule, raise and send what is new."""
    result = await AlertService(db).sweep()
    await _audit(db, user, "alerts.sweep", "alert_rule", uuid.UUID(int=0), raised=result.raised, cleared=result.cleared)
    return SweepOut(ran=result.ran, detail=result.detail, raised=result.raised, cleared=result.cleared, delivered=result.delivered,
                    sources=result.sources)


@router.post("/rules", response_model=RuleOut, status_code=status.HTTP_201_CREATED)
async def create_rule(payload: RuleCreate, user: RequireAlertsManage, db: DB) -> RuleOut:
    rule = await AlertService(db).create_rule(actor=_actor(user), **payload.model_dump())
    await _audit(db, user, "alert_rule.create", "alert_rule", rule.id, name=rule.name, kind=rule.kind)
    return RuleOut.model_validate(rule)


@router.patch("/rules/{rule_id}", response_model=RuleOut)
async def update_rule(rule_id: uuid.UUID, payload: RulePatch, user: RequireAlertsManage, db: DB) -> RuleOut:
    changes = payload.model_dump(exclude_unset=True)
    if "scope_job_key" in changes and not changes["scope_job_key"]:       # "" / null: back to every job
        changes["scope_job_key"], changes["scope_job_name"] = None, None
    rule = await AlertService(db).update_rule(rule_id, changes)
    await _audit(db, user, "alert_rule.update", "alert_rule", rule.id, name=rule.name, fields=sorted(changes))
    return RuleOut.model_validate(rule)


@router.delete("/rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_rule(rule_id: uuid.UUID, user: RequireAlertsManage, db: DB) -> None:
    svc = AlertService(db)
    rule = await svc.get_rule(rule_id)
    name = rule.name
    await svc.delete_rule(rule_id)
    await _audit(db, user, "alert_rule.delete", "alert_rule", rule_id, name=name)


# ── channels ───────────────────────────────────────────────────────────────────────────────────────────────────────────────
@router.get("/channels", response_model=list[ChannelOut])
async def list_channels(user: RequireAlertsRead, db: DB) -> list[ChannelOut]:
    return [ChannelOut.model_validate(c) for c in await AlertService(db).list_channels()]


@router.post("/channels", response_model=ChannelOut, status_code=status.HTTP_201_CREATED)
async def create_channel(payload: ChannelCreate, user: RequireAlertsManage, db: DB) -> ChannelOut:
    ch = await AlertService(db).create_channel(actor=_actor(user), **payload.model_dump())
    await _audit(db, user, "alert_channel.create", "alert_channel", ch.id, name=ch.name, kind=ch.kind)
    return ChannelOut.model_validate(ch)


@router.patch("/channels/{channel_id}", response_model=ChannelOut)
async def update_channel(channel_id: uuid.UUID, payload: ChannelPatch, user: RequireAlertsManage, db: DB) -> ChannelOut:
    changes = payload.model_dump(exclude_unset=True)
    if "scope_job_key" in changes and not changes["scope_job_key"]:
        changes["scope_job_key"], changes["scope_job_name"] = None, None
    ch = await AlertService(db).update_channel(channel_id, changes)
    await _audit(db, user, "alert_channel.update", "alert_channel", ch.id, name=ch.name, fields=sorted(k for k in changes))
    return ChannelOut.model_validate(ch)


@router.delete("/channels/{channel_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_channel(channel_id: uuid.UUID, user: RequireAlertsManage, db: DB) -> None:
    svc = AlertService(db)
    name = (await svc.get_channel(channel_id)).name
    await svc.delete_channel(channel_id)
    await _audit(db, user, "alert_channel.delete", "alert_channel", channel_id, name=name)


@router.post("/channels/{channel_id}/test", response_model=ChannelTestResult)
async def test_channel(channel_id: uuid.UUID, user: RequireAlertsManage, db: DB) -> ChannelTestResult:
    ok, detail, ch = await AlertService(db).test_channel(channel_id, actor=_actor(user))
    await _audit(db, user, "alert_channel.test", "alert_channel", ch.id, name=ch.name, ok=ok)
    return ChannelTestResult(ok=ok, detail=detail, channel=ChannelOut.model_validate(ch))


# ── one alert ──────────────────────────────────────────────────────────────────────────────────────────────────────────────
@router.get("/{alert_id}", response_model=AlertDetail)
async def get_alert(alert_id: uuid.UUID, user: RequireAlertsRead, db: DB) -> AlertDetail:
    svc = AlertService(db)
    return _alert_out(await svc.get_alert(alert_id), svc, detail=True)


@router.post("/{alert_id}/acknowledge", response_model=AlertDetail)
async def acknowledge_alert(alert_id: uuid.UUID, payload: AlertAction, user: RequireAlertsAck, db: DB) -> AlertDetail:
    svc = AlertService(db)
    a = await svc.acknowledge(alert_id, actor=_actor(user), note=payload.note)
    await _audit(db, user, "alert.acknowledge", "alert", a.id, rule=a.rule_name, job=a.job_name)
    return _alert_out(a, svc, detail=True)


@router.post("/{alert_id}/resolve", response_model=AlertDetail)
async def resolve_alert(alert_id: uuid.UUID, payload: AlertAction, user: RequireAlertsAck, db: DB) -> AlertDetail:
    svc = AlertService(db)
    a = await svc.resolve(alert_id, actor=_actor(user), note=payload.note)
    await _audit(db, user, "alert.resolve", "alert", a.id, rule=a.rule_name, job=a.job_name)
    return _alert_out(a, svc, detail=True)
