from datetime import datetime, timedelta
from uuid import uuid4
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Response
from gateway_auth import require_permission
from .. import permissions as P
from ..permissions import require_any_permission
from fastapi.responses import HTMLResponse
from sqlmodel import Session, select
from pydantic import BaseModel

from ..db import get_session
from ..models import Agent, Alert, AlertDestination, AlertRule
from ..pagination import paginate, pagination_params
from ..secrets_crypto import encrypt_secret
from .. import alert_actions, alert_delivery, alert_rules

router = APIRouter(prefix="/api/alerts", tags=["alerts"])


class ActionIn(BaseModel):
    by: str  # who's performing the action — the frontend sends the active persona's identity


def _date_bounds(from_date: Optional[str], to_date: Optional[str]) -> tuple:
    """Parse a date range into (start, end) datetimes, end exclusive.

    An inclusive end matters: picking the same date in both boxes means
    "that day", and a plain <= against midnight would return nothing.

    Shared by the three list endpoints here so a range means the same
    thing on every tab.
    """
    out = []
    for raw, is_end in ((from_date, False), (to_date, True)):
        if not raw:
            out.append(None)
            continue
        try:
            bound = datetime.fromisoformat(raw)
        except ValueError:
            raise HTTPException(
                400, f"{'to_date' if is_end else 'from_date'} must be ISO-8601 "
                     f"(YYYY-MM-DD or a full timestamp), got {raw!r}")
        if is_end and len(raw) == 10:   # a bare date, so include its whole day
            bound = bound + timedelta(days=1)
        out.append(bound)
    return tuple(out)


def now_str() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M")


@router.get("", dependencies=[Depends(require_permission(P.ALERTS))])
def list_alerts(
    response: Response, state: Optional[str] = None,
    from_date: Optional[str] = None, to_date: Optional[str] = None,
    session: Session = Depends(get_session),
    page: dict = Depends(pagination_params),
) -> List[Alert]:
    """§0.113, GAP_REPORT — the same real, keyset-pagination fix as
    runs.py/traces.py's own identical gap; see pagination.py's own
    module docstring for the full real reasoning. The same real,
    honest, stated trade-off as agents.py's own identical fix: Alert
    has no real created_at column of its own (only `ts`, a real,
    existing display STRING with no real ordering guarantee a SQL
    query could trust) — sorted by its own real, unique `id` alone, a
    real, stable, correct pagination order that is NOT chronological."""
    q = select(Alert)
    if state == "open":
        # "Open" means sent to a channel and not yet acted on. An alert
        # nobody was told about is not something anyone is working, and
        # an acknowledged one has moved to its own tab.
        q = q.where(Alert.state == "open", Alert.acked == False,  # noqa: E712
                    Alert.delivered_at.is_not(None))
    elif state == "acknowledged":
        # Acknowledged but not fixed: someone is on it.
        q = q.where(Alert.state == "open", Alert.acked == True)  # noqa: E712
    elif state == "undelivered":
        # Not a tab — the count behind the warning. Without somewhere to
        # see these, an alert that reached no channel would simply vanish
        # from the UI, which is the most dangerous way to lose one.
        q = q.where(Alert.state == "open", Alert.delivered_at.is_(None))
    elif state:
        q = q.where(Alert.state == state)

    # Filtered in SQL, not in the client: this endpoint is paginated, so
    # narrowing a fetched page in the browser would hide every matching
    # alert on later pages.
    #
    # Compared on `ts`, which is a display STRING — but a zero-padded
    # "YYYY-MM-DD HH:MM" one, so lexicographic order IS chronological
    # order and a range comparison is sound. Alert has no typed
    # timestamp column to use instead.
    start, end = _date_bounds(from_date, to_date)
    if start:
        q = q.where(Alert.ts >= start.strftime("%Y-%m-%d %H:%M"))
    if end:
        q = q.where(Alert.ts < end.strftime("%Y-%m-%d %H:%M"))

    return paginate(session, Alert, q, limit=page["limit"], cursor=page["cursor"],
                     sort_col=Alert.id, id_col=Alert.id, response=response)

# --- destinations ---------------------------------------------------------
#
# Alerts existed only inside the app. A destination is a Slack or Teams
# channel someone actually watches, plus the filters deciding which
# alerts reach it.


class DestinationIn(BaseModel):
    name: str
    kind: str = "slack"  # slack | teams
    webhook_url: Optional[str] = None
    enabled: bool = True
    min_severity: str = "low"
    # None means every agent. Set it to send one agent's alerts to one
    # channel and nothing else.
    agent_id: Optional[str] = None


def _public(d: AlertDestination) -> dict:
    """A destination as the API returns it.

    The webhook URL is deliberately absent. It is a credential — anyone
    holding it can post into that channel — so it is write-only: set it,
    replace it, never read it back. `url_hint` is enough to recognise
    which channel this is.
    """
    return {
        "id": d.id, "name": d.name, "kind": d.kind, "enabled": d.enabled,
        "min_severity": d.min_severity, "agent_id": d.agent_id,
        "url_hint": d.url_hint, "configured": bool(d.webhook_url_encrypted),
        "last_attempt_at": d.last_attempt_at.isoformat() if d.last_attempt_at else None,
        "last_status": d.last_status, "last_detail": d.last_detail,
    }


def _hint(url: str) -> str:
    """Enough of a webhook URL to recognise it, never enough to use it."""
    url = (url or "").strip()
    if not url:
        return ""
    without_scheme = url.split("://", 1)[-1]
    host, _, rest = without_scheme.partition("/")
    tail = rest.rsplit("/", 1)[-1] if rest else ""
    return f"{host}/…/{tail[-6:]}" if tail else host


def _validate(body: DestinationIn) -> None:
    if body.kind not in ("slack", "teams"):
        raise HTTPException(400, "kind must be 'slack' or 'teams'")
    if body.min_severity not in ("low", "medium", "high"):
        raise HTTPException(400, "min_severity must be low, medium or high")
    if body.webhook_url and not body.webhook_url.strip().lower().startswith("https://"):
        # Posting a channel credential over plaintext http would expose
        # it to anything on the path.
        raise HTTPException(400, "webhook_url must be https://")


@router.get("/destinations", dependencies=[Depends(require_permission(P.ALERTS))])
def list_destinations(response: Response, session: Session = Depends(get_session),
                      page: dict = Depends(pagination_params)) -> List[dict]:
    """Channels, paginated. No date filter: a channel is configuration,
    not an event, so "channels created last Tuesday" is not a question
    anyone asks of it."""
    rows = paginate(session, AlertDestination, select(AlertDestination),
                    limit=page["limit"], cursor=page["cursor"],
                    sort_col=AlertDestination.id, id_col=AlertDestination.id,
                    response=response)
    return [_public(d) for d in rows]


@router.post("/destinations", dependencies=[Depends(require_permission(P.ALERTS))])
def create_destination(body: DestinationIn, session: Session = Depends(get_session)) -> dict:
    _validate(body)
    if not body.webhook_url:
        raise HTTPException(400, "webhook_url is required to create a destination")
    if session.exec(select(AlertDestination).where(AlertDestination.name == body.name)).first():
        raise HTTPException(409, f"A destination named {body.name!r} already exists")
    if body.agent_id and not session.get(Agent, body.agent_id):
        raise HTTPException(404, f"No agent {body.agent_id!r}")
    d = AlertDestination(
        name=body.name, kind=body.kind, enabled=body.enabled,
        min_severity=body.min_severity, agent_id=body.agent_id,
        webhook_url_encrypted=encrypt_secret(body.webhook_url.strip()),
        url_hint=_hint(body.webhook_url),
    )
    session.add(d)
    session.commit()
    session.refresh(d)
    return _public(d)


@router.patch("/destinations/{destination_id}", dependencies=[Depends(require_permission(P.ALERTS))])
def update_destination(destination_id: int, body: DestinationIn,
                       session: Session = Depends(get_session)) -> dict:
    _validate(body)
    d = session.get(AlertDestination, destination_id)
    if not d:
        raise HTTPException(404, "No such destination")
    d.name, d.kind, d.enabled = body.name, body.kind, body.enabled
    d.min_severity, d.agent_id = body.min_severity, body.agent_id
    # Omitting the URL keeps the existing one. Since it is never
    # readable, a UI cannot round-trip it, and clearing it on every edit
    # would silently disable the channel.
    if body.webhook_url:
        d.webhook_url_encrypted = encrypt_secret(body.webhook_url.strip())
        d.url_hint = _hint(body.webhook_url)
    session.add(d)
    session.commit()
    session.refresh(d)
    return _public(d)


@router.delete("/destinations/{destination_id}", dependencies=[Depends(require_permission(P.ALERTS))])
def delete_destination(destination_id: int, session: Session = Depends(get_session)) -> dict:
    d = session.get(AlertDestination, destination_id)
    if not d:
        raise HTTPException(404, "No such destination")
    session.delete(d)
    session.commit()
    return {"deleted": destination_id}


@router.post("/destinations/{destination_id}/test", dependencies=[Depends(require_permission(P.ALERTS))])
def test_destination(destination_id: int, session: Session = Depends(get_session)) -> dict:
    """Post a real message to the channel.

    Worth having as its own action: the alternative is waiting for a
    real alert to find out the URL was wrong, and a channel nobody has
    ever successfully posted to is indistinguishable from one that is
    simply quiet.
    """
    d = session.get(AlertDestination, destination_id)
    if not d:
        raise HTTPException(404, "No such destination")

    class _Probe:
        id = "test"
        sev = "low"
        rule = "test message"
        agent_id = d.agent_id or "-"
        agent_name = "InAiEra Agent Platform"
        summary = (f"Test message for destination {d.name!r}. "
                   "If you can read this, alerts will reach this channel.")
        ts = now_str()

    outcome = alert_delivery.deliver(d, _Probe())
    d.last_attempt_at = datetime.utcnow()
    d.last_status = "ok" if outcome["ok"] else "failed"
    d.last_detail = outcome["detail"][:400]
    session.add(d)
    session.commit()
    # A failed test is a 200 carrying the reason, not a 500: the request
    # to send was handled correctly, the channel is what refused.
    return {"delivered": outcome["ok"], "detail": outcome["detail"], **_public(d)}


class AlertIn(BaseModel):
    sev: str = "medium"
    rule: str
    agent_id: str
    summary: str


@router.post("", dependencies=[Depends(require_permission(P.ALERTS))])
def raise_alert(body: AlertIn, session: Session = Depends(get_session)) -> dict:
    """Raise an alert and fan it out to every matching destination.

    Stated plainly: nothing in this platform raises alerts automatically
    yet — before this, the only rows came from seed.py. This is the
    entry point a rule engine, a monitor or an operator calls, and it is
    what makes destinations testable end to end rather than a
    configuration screen with nothing behind it.
    """
    if body.sev not in ("low", "medium", "high"):
        raise HTTPException(400, "sev must be low, medium or high")
    agent = session.get(Agent, body.agent_id)
    if not agent:
        raise HTTPException(404, f"No agent {body.agent_id!r}")

    alert = Alert(
        id=f"al-{uuid4().hex[:10]}", sev=body.sev, rule=body.rule,
        agent_id=agent.id, agent_name=agent.name, state="open", acked=False,
        ts=now_str(), summary=body.summary, history=[],
    )
    session.add(alert)
    session.commit()
    session.refresh(alert)

    destinations = session.exec(select(AlertDestination)).all()
    # Delivery never fails the alert: an undeliverable alert is still a
    # real alert, and losing it because a channel was down would be the
    # worst possible trade.
    deliveries = alert_delivery.fan_out(session, alert, destinations)
    # fan_out commits, which expires this instance; SQLModel's dump reads
    # __dict__ without triggering a reload, so without the refresh the
    # caller gets `"alert": {}` back and no indication anything is wrong.
    session.refresh(alert)
    return {"alert": alert.model_dump(), "delivered_to": deliveries}



# --- rules ---------------------------------------------------------------
#
# Rules are stored as data, so they can be added, tuned and scoped to one
# agent from the UI rather than by editing Python and redeploying.

KIND_HELP = {
    "error_rate": {"label": "Error rate", "threshold": "percent of runs failed",
                   "desc": "Fires when an agent fails more than this share of its runs."},
    "denied_burst": {"label": "Denied burst", "threshold": "number of denials",
                     "desc": "Repeated authorization denials — a misconfigured sub-agent or a revoked entitlement."},
    "guardrail_block": {"label": "Guardrail block", "threshold": "number of blocks",
                        "desc": "Moderation refusing messages — either probing, or a rule that is too broad."},
    "step_limit": {"label": "Step limit", "threshold": "number of runs",
                   "desc": "A reasoning loop that exhausted its step budget without answering."},
    "latency_spike": {"label": "Latency spike", "threshold": "multiple of the agent's own baseline",
                      "desc": "p95 latency against this agent's preceding history, not a fixed number."},
    "cost_spike": {"label": "Cost spike", "threshold": "multiple of the agent's own daily rate",
                   "desc": "Spend against this agent's own recent rate."},
    "quality_breach": {"label": "Quality breach", "threshold": "minimum percent passing",
                       "desc": "The agent's latest eval run came in below this bar."},
}


class RuleIn(BaseModel):
    name: str
    kind: str
    severity: str = "medium"
    threshold: float = 1
    window_seconds: int = 0
    min_runs: int = 20
    fail_classes: List[str] = []
    agent_id: Optional[str] = None
    enabled: bool = True


def _rule_public(r: AlertRule, agent_name: Optional[str] = None) -> dict:
    return {
        "id": r.id, "name": r.name, "kind": r.kind, "severity": r.severity,
        "threshold": r.threshold, "window_seconds": r.window_seconds,
        "min_runs": r.min_runs, "fail_classes": r.fail_classes or [],
        "agent_id": r.agent_id, "agent_name": agent_name,
        "enabled": r.enabled,
    }


def _validate_rule(body: RuleIn) -> None:
    if body.kind not in KIND_HELP:
        raise HTTPException(400, f"kind must be one of {', '.join(KIND_HELP)}")
    if body.severity not in ("low", "medium", "high"):
        raise HTTPException(400, "severity must be low, medium or high")
    if body.threshold < 0:
        raise HTTPException(400, "threshold must be 0 or more")
    if body.window_seconds < 0:
        raise HTTPException(400, "window_seconds must be 0 or more (0 means the default)")
    if body.min_runs < 1:
        # A rate over zero runs is a division by zero; over one run it is
        # noise. Refusing here is clearer than quietly clamping.
        raise HTTPException(400, "min_runs must be at least 1")
    if not body.name.strip():
        raise HTTPException(400, "name is required")


@router.get("/rules/kinds", dependencies=[Depends(require_permission(P.ALERTS))])
def rule_kinds() -> dict:
    """What each kind does and what its threshold means.

    The meaning of `threshold` depends on the kind — a percentage for
    error_rate, a count for denied_burst, a multiple of the agent's own
    baseline for the spike rules. The UI shows this beside the field
    rather than leaving someone to guess.
    """
    return {"kinds": [{"value": k, **v} for k, v in KIND_HELP.items()]}


@router.get("/rules", dependencies=[Depends(require_permission(P.ALERTS))])
def list_rules(response: Response,
               from_date: Optional[str] = None, to_date: Optional[str] = None,
               session: Session = Depends(get_session),
               page: dict = Depends(pagination_params)) -> List[dict]:
    """Rules, paginated and filterable by when they were created.

    AlertRule has a real `created_at`, so unlike the alert list this
    range compares against a typed timestamp rather than a formatted
    string.
    """
    q = select(AlertRule)
    start, end = _date_bounds(from_date, to_date)
    if start:
        q = q.where(AlertRule.created_at >= start)
    if end:
        q = q.where(AlertRule.created_at < end)

    rows = paginate(session, AlertRule, q, limit=page["limit"], cursor=page["cursor"],
                    sort_col=AlertRule.id, id_col=AlertRule.id, response=response)
    names = {a.id: a.name for a in session.exec(select(Agent)).all()}
    return [_rule_public(r, names.get(r.agent_id)) for r in rows]


@router.post("/rules", dependencies=[Depends(require_permission(P.ALERTS))])
def create_rule(body: RuleIn, session: Session = Depends(get_session)) -> dict:
    _validate_rule(body)
    if session.exec(select(AlertRule).where(AlertRule.name == body.name.strip())).first():
        raise HTTPException(409, f"A rule named {body.name.strip()!r} already exists")
    if body.agent_id and not session.get(Agent, body.agent_id):
        raise HTTPException(404, f"No agent {body.agent_id!r}")
    r = AlertRule(name=body.name.strip(), kind=body.kind, severity=body.severity,
                  threshold=body.threshold, window_seconds=body.window_seconds,
                  min_runs=body.min_runs, fail_classes=body.fail_classes,
                  agent_id=body.agent_id, enabled=body.enabled)
    session.add(r)
    session.commit()
    session.refresh(r)
    agent = session.get(Agent, r.agent_id) if r.agent_id else None
    return _rule_public(r, agent.name if agent else None)


@router.patch("/rules/{rule_id}", dependencies=[Depends(require_permission(P.ALERTS))])
def update_rule(rule_id: int, body: RuleIn, session: Session = Depends(get_session)) -> dict:
    _validate_rule(body)
    r = session.get(AlertRule, rule_id)
    if not r:
        raise HTTPException(404, "No such rule")
    clash = session.exec(select(AlertRule).where(AlertRule.name == body.name.strip())).first()
    if clash and clash.id != rule_id:
        raise HTTPException(409, f"A rule named {body.name.strip()!r} already exists")
    if body.agent_id and not session.get(Agent, body.agent_id):
        raise HTTPException(404, f"No agent {body.agent_id!r}")
    r.name, r.kind, r.severity = body.name.strip(), body.kind, body.severity
    r.threshold, r.window_seconds = body.threshold, body.window_seconds
    r.min_runs, r.fail_classes = body.min_runs, body.fail_classes
    r.agent_id, r.enabled = body.agent_id, body.enabled
    session.add(r)
    session.commit()
    session.refresh(r)
    agent = session.get(Agent, r.agent_id) if r.agent_id else None
    return _rule_public(r, agent.name if agent else None)


@router.delete("/rules/{rule_id}", dependencies=[Depends(require_permission(P.ALERTS))])
def delete_rule(rule_id: int, session: Session = Depends(get_session)) -> dict:
    r = session.get(AlertRule, rule_id)
    if not r:
        raise HTTPException(404, "No such rule")
    session.delete(r)
    session.commit()
    return {"deleted": rule_id}


@router.get("/rules/preview", dependencies=[Depends(require_permission(P.ALERTS))])
def preview_rules(session: Session = Depends(get_session)) -> dict:
    """Which conditions are true right now, without raising anything.

    Read-only on purpose: it is how you check what a sweep would do, and
    how you tune a threshold, before alerts start reaching a channel
    people are watching.
    """
    candidates = alert_rules.evaluate(session)
    return {
        "conditions": [
            {"sev": c.sev, "rule": c.rule, "agent_id": c.agent_id,
             "agent_name": c.agent_name, "summary": c.summary,
             # So the caller can see what a sweep would actually create
             # versus what dedup would suppress.
             "already_open": alert_rules.already_open(session, c)}
            for c in candidates
        ],
        "thresholds": {
            "window_hours": alert_rules.WINDOW_HOURS,
            "min_runs": alert_rules.MIN_RUNS,
            "error_rate_pct": alert_rules.ERROR_RATE_PCT,
            "denied_burst": alert_rules.DENIED_BURST,
            "guardrail_blocks": alert_rules.GUARDRAIL_BLOCKS,
            "latency_factor": alert_rules.LATENCY_FACTOR,
            "cost_factor": alert_rules.COST_FACTOR,
            "eval_pass_pct": alert_rules.EVAL_PASS_PCT,
        },
    }


@router.post("/rules/sweep", dependencies=[Depends(require_permission(P.ALERTS))])
def sweep_rules(session: Session = Depends(get_session)) -> dict:
    """Run every rule now: raise what is new, deliver it, resolve what
    has cleared. The scheduler calls the same code every 15 minutes."""
    result = alert_rules.sweep(session)
    result["resolved"] = str(alert_rules.resolve_stale(session))
    return result


def _action_page(title: str, body: str, ok: bool = True) -> HTMLResponse:
    """A small page the clicker sees.

    A bare JSON body would be the easy thing to return, but the reader
    arrived here from a chat card in their browser — they need a
    sentence, not a payload.
    """
    tint = "#0F7B6C" if ok else "#B3261E"
    return HTMLResponse(f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{title}</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
          margin: 0; display: grid; place-items: center; min-height: 100vh;
          background: #F6F7F9; color: #10203A; padding: 16px; }}
  @media (prefers-color-scheme: dark) {{
    body {{ background: #10141C; color: #E8EBF0; }}
    .card {{ background: #1A1F2A !important; }}
  }}
  .card {{ background: #fff; border-radius: 12px; padding: 28px 32px; max-width: 460px;
           box-shadow: 0 1px 3px rgba(16,32,58,.12); }}
  h1 {{ font-size: 18px; margin: 0 0 8px; color: {tint}; }}
  p {{ margin: 0; font-size: 14px; opacity: .85; }}
</style></head>
<body><div class="card"><h1>{title}</h1><p>{body}</p></div></body></html>""",
        status_code=200 if ok else 403)


@router.get("/{alert_id}/{action}/via-link", dependencies=[Depends(require_permission(P.ALERTS))])
def act_via_link(alert_id: str, action: str, token: str = "",
                 session: Session = Depends(get_session)):
    """Acknowledge or resolve an alert from a Slack or Teams card.

    The link is signed, because a URL posted in a channel is visible to
    everyone there and gets copied and forwarded. Without the signature
    anyone who saw one could resolve any alert by editing the id.

    Who clicked is deliberately not claimed: a link click carries no
    identity, so the history records the channel rather than inventing a
    name nothing verified.
    """
    if action not in alert_actions.ACTIONS:
        raise HTTPException(404, "Unknown action")
    if not alert_actions.verify(alert_id, action, token):
        return _action_page(
            "That link is not valid",
            "The signature did not match. Links are signed per alert and per "
            "action, so one cannot be edited into another. Open the alert in "
            "the platform instead.", ok=False)

    a = session.get(Alert, alert_id)
    if not a:
        # An alert is deleted only when its agent is, so say that rather
        # than leaving someone to wonder whether the link was wrong.
        return _action_page(
            "That alert is gone",
            "Alerts are removed when their agent is deleted, so this one went with it. "
            "Nothing to acknowledge — the card in your channel is a record of something "
            "that was reported before the agent was removed.", ok=False)

    if action == "acknowledge":
        # Resolving closes the alert, so a later Acknowledge from the same
        # card has nothing left to acknowledge.
        if a.state == "resolved":
            return _action_page("Already resolved",
                                f"{a.rule} on {a.agent_name} was already resolved, "
                                "so it cannot be acknowledged.")
        if a.acked:
            return _action_page("Already acknowledged",
                                f"{a.rule} on {a.agent_name} was already acknowledged.")
        a.acked = True
        a.history = [*a.history, {"action": "Acknowledged", "by": "chat channel",
                                  "ts": now_str()}]
        title, body = "Acknowledged", (
            f"{a.rule} on {a.agent_name} has moved to the Acknowledged tab. "
            "Acknowledging says someone is looking at it; resolving says it is fixed. "
            "It stays there until someone resolves it.")
    else:
        if a.state == "resolved":
            return _action_page("Already resolved",
                                f"{a.rule} on {a.agent_name} was already resolved.")
        a.state = "resolved"
        a.history = [*a.history, {"action": "Resolved", "by": "chat channel",
                                  "ts": now_str()}]
        title, body = "Resolved", (
            f"{a.rule} on {a.agent_name} has moved to the Resolved tab. If the "
            "condition is still true, the rules will raise it again on the next sweep.")

    session.add(a)
    session.commit()
    return _action_page(title, body)


@router.get("/{alert_id}", dependencies=[Depends(require_permission(P.ALERTS))])
def get_alert(alert_id: str, session: Session = Depends(get_session)) -> Alert:
    a = session.get(Alert, alert_id)
    if not a:
        raise HTTPException(404, "Alert not found")
    return a


@router.post("/{alert_id}/acknowledge", dependencies=[Depends(require_permission(P.ALERTS))])
def acknowledge_alert(alert_id: str, body: ActionIn, session: Session = Depends(get_session)) -> Alert:
    a = session.get(Alert, alert_id)
    if not a:
        raise HTTPException(404, "Alert not found")
    if a.state == "resolved":
        raise HTTPException(409, "This alert is already resolved, so it cannot be acknowledged")
    a.acked = True
    a.history = [*a.history, {"action": "Acknowledged", "by": body.by, "ts": now_str()}]
    session.add(a)
    session.commit()
    session.refresh(a)
    return a


@router.post("/{alert_id}/resolve", dependencies=[Depends(require_permission(P.ALERTS))])
def resolve_alert(alert_id: str, body: ActionIn, session: Session = Depends(get_session)) -> Alert:
    a = session.get(Alert, alert_id)
    if not a:
        raise HTTPException(404, "Alert not found")
    a.state = "resolved"
    a.history = [*a.history, {"action": "Resolved", "by": body.by, "ts": now_str()}]
    session.add(a)
    session.commit()
    session.refresh(a)
    return a
