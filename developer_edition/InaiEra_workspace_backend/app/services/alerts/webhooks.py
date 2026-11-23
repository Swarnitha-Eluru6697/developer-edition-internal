"""Slack and Microsoft Teams webhooks: which URLs are accepted, and the messages posted to them.

A channel's webhook URL is a credential: it is checked here when it is saved, kept only in the secrets backend, never returned by the
API and never logged (a failed post logs the channel name and the status, not the URL).
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

import httpx
import structlog

from app.core.config import get_settings
from app.core.egress_policy import EgressPolicyError, assert_host_allowed

log = structlog.get_logger()

# Hosts a webhook of each kind may live on (exact name, or ".suffix" for subdomains).
PROVIDER_HOSTS: dict[str, tuple[str, ...]] = {
    "slack": ("hooks.slack.com", "hooks.slack-gov.com"),
    # Teams: the classic incoming-webhook connector (…webhook.office.com, outlook.office.com) and the Workflows webhook that replaced it.
    "teams": (".webhook.office.com", "outlook.office.com", "outlook.office365.com", ".logic.azure.com", ".powerplatform.com"),
}
SEVERITY_COLOUR = {"high": "#DC2626", "medium": "#F59E0B", "low": "#6B7280"}


class WebhookRejected(ValueError):
    """The URL is not an acceptable webhook for that kind of channel (the message is safe to show to the user)."""


def _host_matches(host: str, allowed: tuple[str, ...]) -> bool:
    return any(host == a or (a.startswith(".") and (host.endswith(a) or host == a[1:])) for a in allowed)


def hint_of(url: str) -> str:
    """A short, non-secret way to recognise a webhook: 'hooks.slack.com/…/x7Q2' (the host and the last 4 characters)."""
    p = urlparse(url)
    tail = (p.path.rstrip("/").rsplit("/", 1)[-1] or "")[-4:]
    return f"{p.hostname}/…/{tail}" if tail else f"{p.hostname}/…"


async def check_webhook_url(kind: str, url: str) -> None:
    """Raises WebhookRejected unless `url` is a usable webhook for `kind`."""
    s = get_settings()
    p = urlparse((url or "").strip())
    host = (p.hostname or "").lower()
    extra = tuple(h.lower() for h in s.alerts_webhook_extra_hosts)
    is_extra = bool(host) and _host_matches(host, extra)
    if p.scheme != "https" and not (p.scheme == "http" and s.environment == "dev" and is_extra):
        raise WebhookRejected("The webhook URL must start with https://.")
    if not host or p.username or p.password:
        raise WebhookRejected("That is not a valid webhook URL.")
    if not (_host_matches(host, PROVIDER_HOSTS[kind]) or is_extra):
        want = " or ".join(h.lstrip(".") for h in PROVIDER_HOSTS[kind][:2])
        raise WebhookRejected(f"A {kind.title()} webhook must be on {want} (or another host your administrator allowed). "
                              f"This one is on {host}.")
    if not is_extra:                    # an operator-listed host is trusted as listed; everything else still passes the egress policy
        try:
            await assert_host_allowed(host)
        except EgressPolicyError as e:
            raise WebhookRejected(str(e)) from None


# ── the messages ───────────────────────────────────────────────────────────────────────────────────────────────────────────
SEVERITY_ICON = {"high": "🚨", "medium": "⚠️", "low": "ℹ️"}
# Adaptive Card text colours for the heading
CARD_COLOUR = {"high": "Attention", "medium": "Warning", "low": "Default"}
# A status update ("acknowledged", "resolved") is coloured by what happened, not by the alert's severity.
TONE_ICON = {"ack": "👀", "resolved": "✅"}
TONE_HEX = {"ack": "#2563EB", "resolved": "#16A34A"}
TONE_CARD = {"ack": "Accent", "resolved": "Good"}


@dataclass(frozen=True)
class Button:
    """A link button on the message (Acknowledge / Resolve): opens `url` in the reader's browser."""
    label: str
    url: str
    style: str = "default"          # default | positive | destructive


@dataclass(frozen=True)
class Message:
    title: str                      # the one-line notification text
    severity: str
    summary: str
    facts: tuple[tuple[str, str], ...]
    heading: str = ""               # the big line on the card (defaults to the title)
    buttons: tuple[Button, ...] = ()
    tone: str = ""                  # "" = an alert (coloured by severity); "ack" / "resolved" = a status update

    @property
    def head(self) -> str:
        icon = TONE_ICON.get(self.tone) or SEVERITY_ICON.get(self.severity, "")
        return f"{icon} {self.heading or self.title}".strip()

    @property
    def hex(self) -> str:
        return TONE_HEX.get(self.tone) or SEVERITY_COLOUR.get(self.severity, "#6B7280")


SLACK_SECTION_LIMIT = 2_900         # Slack refuses a section text over 3000 characters (the whole message is then dropped)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def slack_escape(text: str) -> str:
    """Slack reads `<...>` in message text as a link, a user or a channel mention (`<!channel>`, `<!here>`, `<@U123>`): a job's error message
    must never be able to ping a whole channel or fake a link. Slack's own rule: only & < > are escaped."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _slack_body(m: Message) -> dict:
    colour = m.hex
    blocks: list[dict | None] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{slack_escape(m.head)}*"}},
        {"type": "section", "fields": [{"type": "mrkdwn", "text": f"*{k}*\n{slack_escape(v)}"} for k, v in m.facts]} if m.facts else None,
        {"type": "section", "text": {"type": "mrkdwn", "text": _clip(slack_escape(m.summary), SLACK_SECTION_LIMIT)}},
    ]
    if m.buttons:
        blocks.append({"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": b.label}, "url": b.url,
             **({"style": "primary"} if b.style == "positive" else {"style": "danger"} if b.style == "destructive" else {})}
            for b in m.buttons]})
    return {"text": slack_escape(f"[{m.severity.upper()}] {m.title}"), "attachments": [{"color": colour, "blocks": [x for x in blocks if x]}]}


def _teams_message_card(m: Message) -> dict:
    return {
        "@type": "MessageCard", "@context": "https://schema.org/extensions",
        "summary": m.title, "themeColor": m.hex.lstrip("#"),
        "title": m.head,
        "sections": [{"text": m.summary, "facts": [{"name": k, "value": v} for k, v in m.facts]}],
        **({"potentialAction": [{"@type": "OpenUri", "name": b.label, "targets": [{"os": "default", "uri": b.url}]} for b in m.buttons]}
           if m.buttons else {}),
    }


def _teams_adaptive_card(m: Message) -> dict:
    """What a Teams Workflows webhook expects (the classic connector's MessageCard is not understood there)."""
    return {"type": "message", "attachments": [{
        "contentType": "application/vnd.microsoft.card.adaptive", "contentUrl": None,
        "content": {
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "type": "AdaptiveCard", "version": "1.4",
            "body": [
                {"type": "TextBlock", "weight": "Bolder", "size": "ExtraLarge", "wrap": True, "color": TONE_CARD.get(m.tone) or CARD_COLOUR.get(m.severity, "Default"), "text": m.head},
                {"type": "FactSet", "facts": [{"title": k, "value": v} for k, v in m.facts]},
                {"type": "TextBlock", "wrap": True, "text": m.summary},
            ],
            **({"actions": [{"type": "Action.OpenUrl", "title": b.label, "url": b.url,
                             **({"style": b.style} if b.style != "default" else {})} for b in m.buttons]} if m.buttons else {}),
        },
    }]}


def body_for(kind: str, url: str, m: Message) -> dict:
    if kind == "slack":
        return _slack_body(m)
    host = (urlparse(url).hostname or "").lower()
    return _teams_message_card(m) if host.endswith(".office.com") or host.endswith(".office365.com") else _teams_adaptive_card(m)


@dataclass(frozen=True)
class SendResult:
    ok: bool
    detail: str


async def send(kind: str, url: str, message: Message, *, client: httpx.AsyncClient | None = None) -> SendResult:
    """Post `message`; never raises. The detail is short and safe to store and show (no URL)."""
    timeout = get_settings().alerts_webhook_timeout_seconds
    try:
        await check_webhook_url(kind, url)
    except WebhookRejected as e:
        return SendResult(False, str(e))
    own = client is None
    http = client or httpx.AsyncClient()
    try:
        resp = await http.post(url, json=body_for(kind, url, message), timeout=timeout, follow_redirects=False)
    except httpx.TimeoutException:
        return SendResult(False, f"timed out after {timeout:g} s")
    except httpx.HTTPError as e:
        return SendResult(False, f"could not reach the webhook ({type(e).__name__})")
    finally:
        if own:
            await http.aclose()
    body = (resp.text or "").strip().replace("\n", " ")[:160]
    # Slack answers 200 "ok"; the classic Teams connector 200 "1"; Workflows 200/202. Anything else is a refusal.
    if 200 <= resp.status_code < 300:
        return SendResult(True, "delivered")
    return SendResult(False, f"{resp.status_code} {body}".strip())
