"""The Acknowledge / Resolve buttons in a Slack / Teams alert message.

  GET  /api/v1/dataconnect/alerts/actions/{token}   a small confirmation page (what will happen, and one button)
  POST /api/v1/dataconnect/alerts/actions/{token}   does it, and says so

NOT behind the gateway on purpose (app/api/router.py mounts it on the public router, like health): the person clicking is in Slack / Teams,
not signed in to DataConnect, so the credential is the SIGNED
token in the link (app/services/alerts/links.py): one alert, one action, one channel, expiring. The link opens a confirmation page
first, and only the button on that page changes anything: mail / chat scanners and link previews fetch URLs with GET and must never
acknowledge or resolve an alert by doing so. The action is recorded as done by the channel ("Teams channel <name>").
Not in the OpenAPI document (a page for a browser, not an API). Ported from the Workspace (app/api/alert_actions.py).

Deployment: behind Envoy this path must be exposed WITHOUT the login (docs/ALERTS_IMPLEMENTATION_GUIDE.md, decision D2). Until it is, set no
DATACONNECT_ALERTS_PUBLIC_URL there: the messages then carry no buttons and alerts are acknowledged on the Alerts page.
"""
from __future__ import annotations

import html
import uuid

import structlog
from fastapi import APIRouter, Depends, Response
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import record_audit
from app.core.errors import DataConnectError
from app.db.session import get_session
from app.models import Alert
from app.services.alerts import links
from app.services.alerts.service import AlertService

log = structlog.get_logger(__name__)
router = APIRouter(include_in_schema=False)

HEADERS = {
    "Cache-Control": "no-store", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'",
}
VERB = {"acknowledge": ("Acknowledge", "acknowledged"), "resolve": ("Resolve", "resolved")}
CHANNEL = {"teams": "Teams", "slack": "Slack"}

_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>
body{{margin:0;background:#F9FAFB;color:#1F2937;font:15px/1.5 Inter,system-ui,-apple-system,Segoe UI,Roboto,sans-serif}}
main{{max-width:560px;margin:8vh auto;padding:0 20px}}
.card{{background:#fff;border:1px solid #E5E7EB;border-radius:14px;padding:26px 28px;box-shadow:0 1px 2px rgba(16,32,58,.06)}}
h1{{font-size:20px;margin:0 0 6px}} .eyebrow{{font-size:11px;letter-spacing:.12em;text-transform:uppercase;color:#C2410C;font-weight:600;margin-bottom:8px}}
.meta{{color:#6B7280;font-size:13px;margin:4px 0 14px}} .summary{{background:#F9FAFB;border:1px solid #E5E7EB;border-radius:10px;padding:10px 12px;margin:14px 0;overflow-wrap:anywhere}}
button{{background:#F97316;color:#fff;border:0;border-radius:9px;padding:10px 18px;font:600 14px inherit;cursor:pointer}}
.ok{{color:#15803D}} .bad{{color:#B91C1C}}</style></head><body><main><div class="card">
<div class="eyebrow">InAiEra DataConnect · Alerts</div>{body}</div></main></body></html>"""


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(_PAGE.format(title=html.escape(title), body=body), status_code=status, headers=HEADERS)


def _problem(message: str, status: int) -> HTMLResponse:
    return _page("Alert link", f'<h1 class="bad">This link cannot be used</h1><p>{html.escape(message)}</p>'
                               '<p class="meta">Open the alert in DataConnect (Alerts page) to acknowledge or resolve it there.</p>', status)


def _describe(a: Alert) -> str:
    return (f'<div class="meta">{html.escape(a.severity.upper())} · {html.escape(a.pipeline_name)} · currently <b>{html.escape(a.state)}</b></div>'
            f'<div class="summary">{html.escape(a.summary)}</div>')


async def _load(token: str, db: AsyncSession) -> tuple[links.LinkClaims, Alert] | HTMLResponse:
    try:
        claims = links.read_token(token)
    except links.LinkExpired:
        return _problem("It has expired.", 410)
    except links.LinkInvalid:
        return _problem("It is not valid.", 400)
    try:
        found = await db.get(Alert, str(uuid.UUID(claims.alert_id)))
    except ValueError:
        found = None
    if found is None:
        return _problem("That alert no longer exists.", 404)
    # The organization is the alert's own: the signed link names one alert, so it cannot reach another organization's.
    alert = await AlertService(db, found.org_id).get_alert(str(found.id))
    return claims, alert


def _already(claims: links.LinkClaims, a: Alert) -> bool:
    """Nothing left to do: it is resolved, or it is being acknowledged and already is."""
    return a.state == "resolved" or (claims.action == "acknowledge" and a.state == "acknowledged")


@router.get("/alerts/actions/{token}")
async def confirm(token: str, db: AsyncSession = Depends(get_session)) -> Response:
    loaded = await _load(token, db)
    if isinstance(loaded, HTMLResponse):
        return loaded
    claims, a = loaded
    verb, done = VERB[claims.action]
    if _already(claims, a):
        return _page("Alert", f'<h1>Already {html.escape(a.state)}</h1>{_describe(a)}<p class="meta">Nothing more to do.</p>')
    return _page(f"{verb} alert", f'<h1>{verb} this alert?</h1>{_describe(a)}'
                                   f'<form method="post"><button type="submit">{verb}</button></form>'
                                   f'<p class="meta">It will be recorded as {done} by the {html.escape(CHANNEL.get(claims.channel_kind, "chat"))} channel '
                                   f'“{html.escape(claims.channel_name)}”.</p>')


@router.post("/alerts/actions/{token}")
async def perform(token: str, db: AsyncSession = Depends(get_session)) -> Response:
    loaded = await _load(token, db)
    if isinstance(loaded, HTMLResponse):
        return loaded
    claims, a = loaded
    verb, done = VERB[claims.action]
    if _already(claims, a):                                      # a second click, or someone got there first: say so, change nothing
        return _page("Alert", f'<h1>Already {html.escape(a.state)}</h1>{_describe(a)}<p class="meta">Nothing more to do.</p>')
    actor = f"{CHANNEL.get(claims.channel_kind, 'Chat')} channel {claims.channel_name}"
    svc = AlertService(db, a.org_id)
    note = "via the button in the channel message"
    try:
        if claims.action == "acknowledge":
            a = await svc.acknowledge(a.id, actor=actor, note=note)
        else:
            a = await svc.resolve(a.id, actor=actor, note=note)
    except DataConnectError:                                     # already resolved by someone else meanwhile
        return _page("Alert", f'<h1>Already {html.escape(a.state)}</h1>{_describe(a)}')
    await db.commit()
    await record_audit(org_id=a.org_id, actor=actor, action=done, resource_type="alert", resource_id=str(a.id),
                       after_state={"via": "channel link", "channel": claims.channel_name, "kind": claims.channel_kind})
    log.info("alerts.action_link", action=claims.action, alert_id=str(a.id), channel=claims.channel_name)
    return _page(f"Alert {done}", f'<h1 class="ok">Alert {done}</h1>{_describe(a)}<p class="meta">You can close this page.</p>')
