"""Alerts: trouble with the organization's pipelines, the rules that raise it, and the Slack / Teams channels it is posted to.

  GET    /alerts?state=open|acknowledged|resolved&from=&to=&limit=&offset=   a page of alerts (newest first)
  GET    /alerts/counts                                                      how many per tab (+ open alerts no channel received)
  GET    /alerts/{id}                                                        one alert with its history
  POST   /alerts/{id}/acknowledge | /resolve                                 (dataconnect:alerts:respond)
  GET    /alerts/rules, /rules/kinds, /rules/breaching;  POST /rules, PATCH /rules/{id}, DELETE /rules/{id};  POST /rules/run
  GET    /alerts/channels;  POST /channels, PATCH /channels/{id}, DELETE /channels/{id};  POST /channels/{id}/test

Reading needs dataconnect:alerts:view, acknowledging / resolving dataconnect:alerts:respond, everything that changes configuration or
sends dataconnect:alerts:manage (permissions.json). A channel's webhook URL is accepted on create / edit and is never returned (only a
short hint such as "hooks.slack.com/…/x7Q2"). Every change is audited.
The Acknowledge / Resolve buttons in a Slack / Teams message are a separate, signed, unauthenticated route: app/api/routes/alert_actions.py.
"""

from datetime import UTC, date, datetime, time, timedelta

from fastapi import APIRouter, Depends, Query, status
from gateway_auth import Principal
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actor import get_actor
from app.core.audit import record_audit
from app.core.permissions import require
from app.core.tenant import get_org_id
from app.db.session import get_session
from app.schemas.alerts import (
    AlertAction,
    AlertCounts,
    AlertDetail,
    AlertOut,
    AlertsPage,
    Breaching,
    ChannelCreate,
    ChannelOut,
    ChannelPatch,
    ChannelTestResult,
    RuleCreate,
    RuleKindInfo,
    RuleOut,
    RulePatch,
    SweepOut,
)
from app.services.alerts.kinds import DEFAULT_WINDOW_SECONDS, RULE_KINDS
from app.services.alerts.service import AlertService

router = APIRouter()

View = Depends(require("dataconnect:alerts:view"))
Respond = Depends(require("dataconnect:alerts:respond"))      # acknowledge / resolve
Manage = Depends(require("dataconnect:alerts:manage"))        # rules, channels, send test, run rules now


def alert_out(a, *, detail: bool = False):
    action, actor, at = AlertService.last_event(a)
    base = {
        "id": str(a.id), "rule_id": str(a.rule_id) if a.rule_id else None, "rule_name": a.rule_name, "kind": a.kind, "severity": a.severity,
        "pipeline_id": a.pipeline_id, "pipeline_name": a.pipeline_name, "direction": a.direction, "summary": a.summary, "state": a.state, "delivery": a.delivery,
        "raised_at": a.raised_at, "sent_at": a.sent_at, "resolved_at": a.resolved_at, "last_action": action, "last_actor": actor, "last_at": at}
    if detail:
        return AlertDetail.model_validate({**base, "events": [{"action": e.action, "actor": e.actor, "note": e.note, "at": e.at} for e in a.events]})
    return AlertOut.model_validate(base)


def _day_start(d: date | None) -> datetime | None:
    return datetime.combine(d, time.min, tzinfo=UTC) if d else None


async def _audit(session: AsyncSession, org_id: str, actor: str, action: str, resource_type: str, resource_id: str | None, **after) -> None:
    await session.commit()                     # the change itself first: an audit row never describes something that was rolled back
    await record_audit(org_id=org_id, actor=actor, action=action, resource_type=resource_type, resource_id=resource_id,
                       after_state=after or None)


# ── alerts ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
@router.get("", response_model=AlertsPage, summary="A page of alerts in one tab")
async def list_alerts(
    state: str = Query("open", pattern="^(open|acknowledged|resolved)$"),
    date_from: date | None = Query(None, alias="from"), date_to: date | None = Query(None, alias="to"),  # noqa: B008 - FastAPI's Query, like Depends
    limit: int = Query(10, ge=1, le=100), offset: int = Query(0, ge=0),
    org_id: str = Depends(get_org_id), session: AsyncSession = Depends(get_session), _principal: Principal = View,
) -> AlertsPage:
    svc = AlertService(session, org_id)
    end = _day_start(date_to)
    alerts, total = await svc.list_alerts(state, start=_day_start(date_from), end=end + timedelta(days=1) if end else None,
                                          limit=limit, offset=offset)
    counts = await svc.counts()
    return AlertsPage(items=[alert_out(a) for a in alerts], total=total, undelivered=counts["undelivered"])


@router.get("/counts", response_model=AlertCounts, summary="How many alerts per tab")
async def alert_counts(org_id: str = Depends(get_org_id), session: AsyncSession = Depends(get_session), _principal: Principal = View) -> AlertCounts:
    return AlertCounts(**await AlertService(session, org_id).counts())


@router.get("/rules/kinds", response_model=list[RuleKindInfo], summary="What an alert rule can watch for")
async def rule_kinds(_principal: Principal = View) -> list[RuleKindInfo]:
    return [RuleKindInfo.model_validate({
        "kind": k, "label": i.label, "description": i.description, "threshold_hint": i.threshold_hint, "uses_window": i.uses_window,
        "uses_min_runs": i.uses_min_runs, "max_threshold": i.max_threshold, "default_window_seconds": DEFAULT_WINDOW_SECONDS if i.uses_window else 0})
        for k, i in RULE_KINDS.items()]


# ── rules ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
@router.get("/rules", response_model=list[RuleOut], summary="The alert rules")
async def list_rules(org_id: str = Depends(get_org_id), session: AsyncSession = Depends(get_session), _principal: Principal = View) -> list[RuleOut]:
    return [RuleOut.model_validate(r) for r in await AlertService(session, org_id).list_rules()]


@router.get("/rules/breaching", response_model=list[Breaching], summary="What breaches a rule right now (nothing is raised by asking)")
async def breaching(org_id: str = Depends(get_org_id), session: AsyncSession = Depends(get_session), _principal: Principal = View) -> list[Breaching]:
    return [Breaching.model_validate({"rule_id": b.rule_id, "rule_name": b.rule_name, "kind": b.kind, "severity": b.severity, "pipeline_id": b.pipeline_id,
                                          "pipeline_name": b.pipeline_name, "direction": b.direction, "summary": b.summary, "state": state})
            for b, state in await AlertService(session, org_id).breaching()]


@router.post("/rules/run", response_model=SweepOut, summary="Run every rule now (\"Run rules now\")")
async def run_rules_now(org_id: str = Depends(get_org_id), actor: str = Depends(get_actor),
                        session: AsyncSession = Depends(get_session), _principal: Principal = Manage) -> SweepOut:
    result = await AlertService(session, org_id).sweep()
    await _audit(session, org_id, actor, "alerts_swept", "alert_rule", None, raised=result.raised, cleared=result.cleared, detail=result.detail)
    return SweepOut(ran=result.ran, detail=result.detail, raised=result.raised, cleared=result.cleared, delivered=result.delivered,
                    sources=result.sources)


@router.post("/rules", response_model=RuleOut, status_code=status.HTTP_201_CREATED, summary="Create an alert rule")
async def create_rule(payload: RuleCreate, org_id: str = Depends(get_org_id), actor: str = Depends(get_actor),
                      session: AsyncSession = Depends(get_session), _principal: Principal = Manage) -> RuleOut:
    rule = await AlertService(session, org_id).create_rule(actor=actor, **payload.model_dump())
    out = RuleOut.model_validate(rule)
    await _audit(session, org_id, actor, "created", "alert_rule", out.id, name=out.name, kind=out.kind, severity=out.severity)
    return out


@router.patch("/rules/{rule_id}", response_model=RuleOut, summary="Change an alert rule (or switch it on / off)")
async def update_rule(rule_id: str, payload: RulePatch, org_id: str = Depends(get_org_id), actor: str = Depends(get_actor),
                      session: AsyncSession = Depends(get_session), _principal: Principal = Manage) -> RuleOut:
    changes = payload.model_dump(exclude_unset=True)
    if "scope_pipeline_id" in changes and not changes["scope_pipeline_id"]:      # "" / null: back to every pipeline
        changes["scope_pipeline_id"], changes["scope_pipeline_name"] = None, None
    rule = await AlertService(session, org_id).update_rule(rule_id, changes)
    out = RuleOut.model_validate(rule)
    await _audit(session, org_id, actor, "updated", "alert_rule", out.id, name=out.name, fields=sorted(changes))
    return out


@router.delete("/rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete an alert rule")
async def delete_rule(rule_id: str, org_id: str = Depends(get_org_id), actor: str = Depends(get_actor),
                      session: AsyncSession = Depends(get_session), _principal: Principal = Manage) -> None:
    svc = AlertService(session, org_id)
    rule = await svc.get_rule(rule_id)
    name, rid = rule.name, str(rule.id)
    await svc.delete_rule(rule_id)
    await _audit(session, org_id, actor, "deleted", "alert_rule", rid, name=name)


# ── channels ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────
@router.get("/channels", response_model=list[ChannelOut], summary="The Slack / Teams channels")
async def list_channels(org_id: str = Depends(get_org_id), session: AsyncSession = Depends(get_session), _principal: Principal = View) -> list[ChannelOut]:
    return [ChannelOut.model_validate(c) for c in await AlertService(session, org_id).list_channels()]


@router.post("/channels", response_model=ChannelOut, status_code=status.HTTP_201_CREATED, summary="Add a Slack / Teams channel")
async def create_channel(payload: ChannelCreate, org_id: str = Depends(get_org_id), actor: str = Depends(get_actor),
                         session: AsyncSession = Depends(get_session), _principal: Principal = Manage) -> ChannelOut:
    ch = await AlertService(session, org_id).create_channel(actor=actor, **payload.model_dump())
    out = ChannelOut.model_validate(ch)
    await _audit(session, org_id, actor, "created", "alert_channel", out.id, name=out.name, kind=out.kind, url=out.url_hint)
    return out


@router.patch("/channels/{channel_id}", response_model=ChannelOut, summary="Change a channel (a blank webhook URL keeps the stored one)")
async def update_channel(channel_id: str, payload: ChannelPatch, org_id: str = Depends(get_org_id), actor: str = Depends(get_actor),
                         session: AsyncSession = Depends(get_session), _principal: Principal = Manage) -> ChannelOut:
    changes = payload.model_dump(exclude_unset=True)
    if "scope_pipeline_id" in changes and not changes["scope_pipeline_id"]:
        changes["scope_pipeline_id"], changes["scope_pipeline_name"] = None, None
    fields = sorted(k for k, v in changes.items() if k != "webhook_url" or v)      # a blank URL keeps the stored one: not a change
    ch = await AlertService(session, org_id).update_channel(channel_id, changes)
    out = ChannelOut.model_validate(ch)
    await _audit(session, org_id, actor, "updated", "alert_channel", out.id, name=out.name, fields=fields)
    return out


@router.delete("/channels/{channel_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Remove a channel")
async def delete_channel(channel_id: str, org_id: str = Depends(get_org_id), actor: str = Depends(get_actor),
                         session: AsyncSession = Depends(get_session), _principal: Principal = Manage) -> None:
    svc = AlertService(session, org_id)
    ch = await svc.get_channel(channel_id)
    name, cid = ch.name, str(ch.id)
    await svc.delete_channel(channel_id)
    await _audit(session, org_id, actor, "deleted", "alert_channel", cid, name=name)


@router.post("/channels/{channel_id}/test", response_model=ChannelTestResult, summary="Send a test message to a channel", name="test_channel")
async def send_channel_test(channel_id: str, org_id: str = Depends(get_org_id), actor: str = Depends(get_actor),
                            session: AsyncSession = Depends(get_session), _principal: Principal = Manage) -> ChannelTestResult:
    ok, detail, ch = await AlertService(session, org_id).test_channel(channel_id, actor=actor)
    out = ChannelOut.model_validate(ch)
    await _audit(session, org_id, actor, "tested", "alert_channel", out.id, name=out.name, ok=ok)
    return ChannelTestResult(ok=ok, detail=detail, channel=out)


# ── one alert ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────
@router.get("/{alert_id}", response_model=AlertDetail, summary="One alert with its history")
async def get_alert(alert_id: str, org_id: str = Depends(get_org_id), session: AsyncSession = Depends(get_session),
                    _principal: Principal = View) -> AlertDetail:
    return alert_out(await AlertService(session, org_id).get_alert(alert_id), detail=True)


@router.post("/{alert_id}/acknowledge", response_model=AlertDetail, summary="Acknowledge an alert (someone is looking at it)")
async def acknowledge_alert(alert_id: str, payload: AlertAction, org_id: str = Depends(get_org_id), actor: str = Depends(get_actor),
                            session: AsyncSession = Depends(get_session), _principal: Principal = Respond) -> AlertDetail:
    a = await AlertService(session, org_id).acknowledge(alert_id, actor=actor, note=payload.note)
    out = alert_out(a, detail=True)
    await _audit(session, org_id, actor, "acknowledged", "alert", out.id, rule=a.rule_name, pipeline=a.pipeline_name)
    return out


@router.post("/{alert_id}/resolve", response_model=AlertDetail, summary="Resolve an alert")
async def resolve_alert(alert_id: str, payload: AlertAction, org_id: str = Depends(get_org_id), actor: str = Depends(get_actor),
                        session: AsyncSession = Depends(get_session), _principal: Principal = Respond) -> AlertDetail:
    a = await AlertService(session, org_id).resolve(alert_id, actor=actor, note=payload.note)
    out = alert_out(a, detail=True)
    await _audit(session, org_id, actor, "resolved", "alert", out.id, rule=a.rule_name, pipeline=a.pipeline_name)
    return out
