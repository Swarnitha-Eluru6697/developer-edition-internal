"""Acknowledge and resolve an alert from the Slack or Teams card itself.

Reading an alert in a channel and then having to go find it in the
platform to acknowledge it is how alerts end up unacknowledged. The card
carries two buttons instead.

These are **link** buttons, not Slack interactive components. That is a
deliberate choice: a Slack interactive button requires the Slack app to
be configured with a request URL that Slack's servers can POST to, which
a platform running on localhost cannot offer. A link button works from
both Slack and Teams with no app configuration at all, and the click is
an ordinary GET from the reader's own browser — which reaches localhost
fine when they are on the same machine.

The link therefore has to be unforgeable on its own, because a URL in a
chat message is visible to everyone in the channel and gets copied,
quoted and forwarded. Each carries an HMAC over (alert id, action) so
one cannot be edited into another, and the signing key is the same
SECRETS_ENCRYPTION_KEY the rest of the platform uses.

What this does NOT attempt is identifying *who* clicked. A link click
carries no identity, so the action is recorded as coming from the
channel rather than from a named person. Claiming otherwise would put a
name in the audit history that nothing verified.
"""
from __future__ import annotations

import hashlib
import hmac
import os
from typing import Optional

# Actions a card may offer. Kept explicit: signing an arbitrary action
# string would let a valid signature be reused for something new later.
ACTIONS = ("acknowledge", "resolve")


def _signing_key() -> bytes:
    """The key links are signed with.

    Falls back to a per-process value when SECRETS_ENCRYPTION_KEY is
    unset, which is the same trade-off secrets_crypto.py already makes
    for local development: links stop working after a restart rather
    than being signed with a known constant.
    """
    configured = os.getenv("SECRETS_ENCRYPTION_KEY")
    if configured:
        return configured.encode()
    # Imported lazily so this module does not force key generation at
    # import time.
    from .secrets_crypto import _get_fernet  # type: ignore[attr-defined]
    return repr(_get_fernet()).encode()


def sign(alert_id: str, action: str) -> str:
    """A token binding one action to one alert."""
    if action not in ACTIONS:
        raise ValueError(f"unknown alert action {action!r}; expected one of {ACTIONS}")
    # The separator matters: without it ("al-1", "23resolve") and
    # ("al-123", "resolve") would hash identically.
    msg = f"{alert_id}\x00{action}".encode()
    return hmac.new(_signing_key(), msg, hashlib.sha256).hexdigest()[:32]


def verify(alert_id: str, action: str, token: str) -> bool:
    """Whether this token really authorises this action on this alert."""
    if action not in ACTIONS:
        return False
    # compare_digest, not ==: a plain comparison leaks how much of the
    # token matched through how long it took to fail.
    return hmac.compare_digest(sign(alert_id, action), token or "")


def base_url() -> str:
    """Where the reader's browser should send the click.

    Configurable because the value that works is the one the *reader*
    can reach, which is not something the server can infer: behind a
    tunnel or a deployment it is a public hostname, and on a laptop it
    is localhost.
    """
    return os.getenv("ALERT_ACTION_BASE_URL", "http://localhost:3000").rstrip("/")


def action_url(alert_id: str, action: str) -> str:
    return (f"{base_url()}/api/alerts/{alert_id}/{action}/via-link"
            f"?token={sign(alert_id, action)}")
