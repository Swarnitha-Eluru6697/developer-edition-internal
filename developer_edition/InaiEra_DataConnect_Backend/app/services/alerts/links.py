"""Signed links for the Acknowledge / Resolve buttons in a Slack / Teams message.

A button cannot carry a login: the person who clicks it is only known to Teams / Slack. So the link carries a SIGNED token instead
(HMAC-SHA256 with ALERTS_ACTION_SECRET): which alert, which action, which channel the message went to, and until when. It can do exactly
that one thing to that one alert, expires, and cannot be altered or forged without the secret. The action is recorded as done "by"
the channel (e.g. "Teams channel dataconnect-alerts"), the same way the Agents app records "chat channel".
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass

from app.core.config import settings

ACTIONS = ("acknowledge", "resolve")


class LinkInvalid(ValueError):
    """The token is not one this service issued (or was altered)."""


class LinkExpired(LinkInvalid):
    """The token was valid but is past its expiry."""


@dataclass(frozen=True)
class LinkClaims:
    alert_id: str
    action: str
    channel_kind: str          # slack | teams
    channel_name: str
    expires_at: int


def enabled() -> bool:
    return bool(settings.alerts_public_url.strip() and settings.alerts_action_secret)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(payload: str) -> str:
    secret = settings.alerts_action_secret or ""
    return _b64(hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest())


def make_token(alert_id: str, action: str, channel_kind: str, channel_name: str, *, now: float | None = None) -> str:
    if action not in ACTIONS:
        raise ValueError(f"unknown action {action!r}")
    ttl = settings.alerts_action_link_ttl_seconds
    body = {"a": alert_id, "x": action, "k": channel_kind, "n": channel_name, "e": int((now if now is not None else time.time()) + ttl)}
    payload = _b64(json.dumps(body, separators=(",", ":")).encode())
    return f"{payload}.{_sign(payload)}"


def read_token(token: str, *, now: float | None = None) -> LinkClaims:
    """The claims of a genuine, unexpired token; LinkInvalid / LinkExpired otherwise."""
    if not settings.alerts_action_secret:
        raise LinkInvalid("links are not enabled")
    payload, _, signature = (token or "").partition(".")
    if not payload or not signature or not hmac.compare_digest(signature, _sign(payload)):
        raise LinkInvalid("bad signature")
    try:
        body = json.loads(_unb64(payload))
        claims = LinkClaims(str(body["a"]), str(body["x"]), str(body["k"]), str(body["n"]), int(body["e"]))
    except (ValueError, KeyError, TypeError):
        raise LinkInvalid("unreadable") from None
    if claims.action not in ACTIONS:
        raise LinkInvalid("unknown action")
    if (now if now is not None else time.time()) > claims.expires_at:
        raise LinkExpired("expired")
    return claims


def action_url(alert_id: str, action: str, channel_kind: str, channel_name: str) -> str:
    base = settings.alerts_public_url.strip().rstrip("/")
    return f"{base}{settings.api_prefix}/alerts/actions/{make_token(alert_id, action, channel_kind, channel_name)}"
