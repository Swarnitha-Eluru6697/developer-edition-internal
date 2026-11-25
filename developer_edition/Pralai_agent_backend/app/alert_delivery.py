"""Deliver alerts to a Slack or Teams channel.

Alerts existed only inside the app: `Alert` rows were displayed on the
Alerts page and nothing ever left the platform. A destination is a
channel someone actually watches, plus the rules for which alerts reach
it — so a high-severity failure on one agent can go to the on-call
channel without every low-severity notice following it there.

Three things this module is careful about, because each is a way to
leak or to lie:

  * An incoming-webhook URL is a credential. Anyone holding one can post
    into that channel. It is stored encrypted with the same
    `encrypt_secret` the platform already uses for MCP tokens, and is
    never returned by the API — a redacted hint goes back instead.
  * Outbound calls go through `safe_http_request`, the same governed
    path the HTTP node and Doc Extractor use. A webhook URL is
    user-supplied, so without that governance a destination pointed at
    an internal address would be a straightforward SSRF.
  * Delivery failures are recorded on the destination rather than
    raised. An alert that could not be delivered must not also destroy
    the alert, and a channel that has been silently failing for a week
    is worth being able to see.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from . import alert_actions
from .egress import SSRFBlocked, safe_http_request
from .secrets_crypto import decrypt_secret

logger = logging.getLogger(__name__)

SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2}


def severity_at_least(sev: str, minimum: str) -> bool:
    """Whether `sev` meets the destination's floor.

    An unknown severity is treated as meeting any floor rather than
    being dropped: a new severity name should over-notify, not silently
    disappear.
    """
    if sev not in SEVERITY_ORDER:
        return True
    return SEVERITY_ORDER[sev] >= SEVERITY_ORDER.get(minimum, 0)


def matches(destination, alert) -> bool:
    """Whether this alert should reach this destination.

    Both filters are AND-ed, and both default to "everything": a
    destination created without filters receives all alerts, which is
    the least surprising default for something someone just set up.
    """
    if not destination.enabled:
        return False
    if not severity_at_least(alert.sev, destination.min_severity or "low"):
        return False
    # A destination scoped to one agent ignores every other agent's
    # alerts — this is what "send alerts only to that channel" needs to
    # mean in practice.
    if destination.agent_id and destination.agent_id != alert.agent_id:
        return False
    return True


def _slack_payload(alert) -> dict:
    emoji = {"high": ":rotating_light:", "medium": ":warning:", "low": ":information_source:"}
    return {
        # `text` is also the notification/fallback line, so it carries
        # the whole summary rather than a title a phone would show alone.
        "text": f"{emoji.get(alert.sev, '')} [{alert.sev.upper()}] {alert.agent_name}: {alert.summary}",
        "blocks": [
            {"type": "section", "text": {
                "type": "mrkdwn",
                "text": f"{emoji.get(alert.sev, '')} *{alert.sev.upper()}* — *{alert.agent_name}*\n{alert.summary}",
            }},
            {"type": "context", "elements": [
                {"type": "mrkdwn", "text": f"rule `{alert.rule}` · alert `{alert.id}` · {alert.ts}"},
            ]},
            # Link buttons, not interactive components: Slack would have
            # to POST back to a request URL for those, which a platform
            # on localhost cannot offer. A url button needs no app
            # configuration and works from any Slack workspace.
            {"type": "actions", "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "Acknowledge"},
                 "url": alert_actions.action_url(alert.id, "acknowledge")},
                {"type": "button", "text": {"type": "plain_text", "text": "Resolve"},
                 "style": "primary",
                 "url": alert_actions.action_url(alert.id, "resolve")},
            ]},
        ],
    }


def _is_workflows_url(url: str) -> bool:
    """Whether this is a Power Automate / Workflows endpoint.

    Microsoft retired Office 365 connectors. The replacement is a
    Workflows (Power Automate) URL, and the two take DIFFERENT payloads:
    a classic connector renders a MessageCard, while a Workflows trigger
    expects an Adaptive Card wrapped in an attachment.

    This distinction is invisible from the response. A Workflows
    endpoint answers 202 Accepted — meaning "the flow was queued" — to a
    MessageCard it will then silently fail to render. So the POST looks
    successful and nothing ever appears in the channel.
    """
    host = (url or "").split("://", 1)[-1].split("/", 1)[0].lower()
    return any(marker in host for marker in (
        "powerplatform.com", "logic.azure.com", "powerautomate.com",
        "azure-apihub.net",
    ))


def _adaptive_card(alert) -> dict:
    """An Adaptive Card, for a Workflows endpoint."""
    colour = {"high": "attention", "medium": "warning", "low": "good"}
    return {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "type": "AdaptiveCard",
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "version": "1.4",
                "body": [
                    {"type": "TextBlock", "size": "Medium", "weight": "Bolder",
                     "color": colour.get(alert.sev, "default"), "wrap": True,
                     "text": f"{alert.sev.upper()} — {alert.agent_name}"},
                    {"type": "TextBlock", "text": alert.summary, "wrap": True},
                    {"type": "FactSet", "facts": [
                        {"title": "Rule", "value": alert.rule},
                        {"title": "Agent", "value": alert.agent_name},
                        {"title": "Severity", "value": alert.sev},
                        {"title": "Raised", "value": str(alert.ts)},
                        {"title": "Alert", "value": alert.id},
                    ]},
                ],
                "actions": [
                    {"type": "Action.OpenUrl", "title": "Acknowledge",
                     "url": alert_actions.action_url(alert.id, "acknowledge")},
                    {"type": "Action.OpenUrl", "title": "Resolve",
                     "url": alert_actions.action_url(alert.id, "resolve")},
                ],
            },
        }],
    }


def _message_card(alert) -> dict:
    """A MessageCard, for a classic Office 365 Incoming Webhook connector."""
    colour = {"high": "D93025", "medium": "E8A33D", "low": "4A90D9"}
    return {
        "@type": "MessageCard",
        "@context": "https://schema.org/extensions",
        "themeColor": colour.get(alert.sev, "4A90D9"),
        "summary": f"[{alert.sev.upper()}] {alert.agent_name}",
        "sections": [{
            "activityTitle": f"**{alert.sev.upper()}** — {alert.agent_name}",
            "activitySubtitle": f"rule {alert.rule} · {alert.ts}",
            "text": alert.summary,
            "facts": [
                {"name": "Alert", "value": alert.id},
                {"name": "Agent", "value": alert.agent_name},
                {"name": "Severity", "value": alert.sev},
            ],
        }],
        "potentialAction": [
            {"@type": "OpenUri", "name": "Acknowledge",
             "targets": [{"os": "default",
                          "uri": alert_actions.action_url(alert.id, "acknowledge")}]},
            {"@type": "OpenUri", "name": "Resolve",
             "targets": [{"os": "default",
                          "uri": alert_actions.action_url(alert.id, "resolve")}]},
        ],
    }


def _teams_payload(alert, url: str = "") -> dict:
    return _adaptive_card(alert) if _is_workflows_url(url) else _message_card(alert)


def payload_for(kind: str, alert, url: str = "") -> dict:
    """The body for this provider. Teams needs the URL too, because the
    two kinds of Teams webhook take different payloads."""
    return _teams_payload(alert, url) if kind == "teams" else _slack_payload(alert)


def deliver(destination, alert, timeout: float = 8.0) -> dict:
    """Post one alert to one destination. Never raises.

    Returns {"ok": bool, "detail": str}. The caller records it on the
    destination; an undeliverable alert is still a real alert and must
    survive the attempt.
    """
    url = decrypt_secret(destination.webhook_url_encrypted)
    if not url:
        return {"ok": False, "detail": "no webhook URL configured"}
    try:
        resp = safe_http_request(
            "POST", url, json=payload_for(destination.kind, alert, url), timeout=timeout)
    except SSRFBlocked as e:
        # Worth its own branch: this is the platform refusing, not the
        # channel being down, and the fix is different.
        return {"ok": False, "detail": f"blocked by egress policy: {e}"}
    except Exception as e:
        return {"ok": False, "detail": f"{type(e).__name__}: {e}"}

    if resp["status_code"] == 202 and _is_workflows_url(url):
        # 202 from a Workflows endpoint means the flow was QUEUED, not
        # that a message appeared. Saying "HTTP 202" alone would read as
        # delivered, which is how a silently-failing channel goes
        # unnoticed.
        return {"ok": True, "detail": "HTTP 202 — accepted by Power Automate (queued)"}
    if resp["status_code"] >= 400:
        # Slack and Teams both answer 200 with a body on success and
        # put the reason in the body on failure, so the body matters
        # more than the code for diagnosing a dead webhook.
        return {"ok": False,
                "detail": f"HTTP {resp['status_code']}: {str(resp.get('text'))[:120]}"}
    return {"ok": True, "detail": f"HTTP {resp['status_code']}"}


def fan_out(session, alert, destinations) -> list:
    """Deliver one alert to every destination that matches it.

    One failing channel must not stop the others, so each is attempted
    independently and the outcome recorded per destination.
    """
    results = []
    reached = []
    for d in destinations:
        if not matches(d, alert):
            continue
        outcome = deliver(d, alert)
        d.last_attempt_at = datetime.utcnow()
        d.last_status = "ok" if outcome["ok"] else "failed"
        d.last_detail = outcome["detail"][:400]
        if outcome["ok"]:
            reached.append(d.name)
        else:
            logger.warning("Alert %s could not be delivered to %s: %s",
                           alert.id, d.name, outcome["detail"])
        session.add(d)
        results.append({"destination": d.name, **outcome})

    if reached:
        # Recorded on the alert, not just on the destination: the Open
        # tab means "sent to a channel and not yet acted on", so this is
        # part of the alert's own state. Names are merged rather than
        # replaced so a retry to a second channel does not erase the
        # first.
        alert.delivered_at = alert.delivered_at or datetime.utcnow()
        alert.delivered_to = sorted(set([*(alert.delivered_to or []), *reached]))
        session.add(alert)
    session.commit()
    return results
