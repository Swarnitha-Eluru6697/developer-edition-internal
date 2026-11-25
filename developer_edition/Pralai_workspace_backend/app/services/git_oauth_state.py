"""OAuth state + PKCE store for the Git connect flow (WS-GIT-009).

Redis-backed, single-use, short TTL. `state` is the CSRF defense — opaque,
unguessable, checked (and deleted) exactly once on callback, so a captured
or replayed callback URL can't be reused. The PKCE code_verifier is
generated here and never leaves the server; only its SHA-256 challenge goes
out in the authorize URL. Our OAuth clients are confidential (the server
holds client_secret, so PKCE isn't load-bearing the way it is for a public
client), but sending it is free for providers that support it and is
exactly what the required action asks for as defense in depth against
authorization-code interception.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets

from app.services.execution_router import get_redis

_TTL_SECONDS = 600  # 10 minutes — long enough to sit on a real consent screen


def generate_pkce_pair() -> tuple[str, str]:
    """Returns (code_verifier, code_challenge); challenge is S256(verifier) per RFC 7636."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


BINDING_COOKIE = "ws_git_oauth"
BINDING_COOKIE_MAX_AGE = _TTL_SECONDS


def new_browser_binding() -> tuple[str, str]:
    """(nonce for the browser's cookie, hash to store with the state).

    Binds a flow to the browser that started it: the callback only accepts
    a state whose stored hash matches that browser's cookie. Without it, an
    attacker could start a flow on their own account and get a victim to
    finish it, attaching the victim's provider token to the attacker's
    account (OAuth account-linking CSRF)."""
    nonce = secrets.token_urlsafe(32)
    return nonce, hashlib.sha256(nonce.encode()).hexdigest()


def binding_matches(nonce: str | None, stored_hash: str | None) -> bool:
    if not nonce or not stored_hash:
        return False
    return secrets.compare_digest(hashlib.sha256(nonce.encode()).hexdigest(), stored_hash)


async def store_oauth_state(
    state: str, *, user_id: str, provider: str, redirect_uri: str,
    code_verifier: str, browser_binding: str, instance_url: str | None = None,
) -> None:
    r = get_redis()
    await r.set(
        f"git-oauth-state:{state}",
        json.dumps({
            "user_id": user_id,
            "provider": provider,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
            "instance_url": instance_url,
            "browser_binding": browser_binding,
        }),
        ex=_TTL_SECONDS,
    )


async def consume_oauth_state(state: str) -> dict | None:
    """Look up and immediately delete — single-use, so a captured callback
    URL (code + state) can't be replayed once the real exchange happens."""
    r = get_redis()
    # GETDEL: atomic, so two concurrent callbacks can't both consume it.
    raw = await r.getdel(f"git-oauth-state:{state}")
    if raw is None:
        return None
    return json.loads(raw)
