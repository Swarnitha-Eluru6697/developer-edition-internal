"""Which model answers the codegen assistant: the in-perimeter gateway (default), or OpenAI / Anthropic with the admin's own key.

The choice is org-wide and admin-only (Settings → Codegen agent). It is stored in the secrets backend — the API key lives NOWHERE
else: not in Redis, not in the database, never in a response (only a masked form), never in an audit record or a log line. Every
reader goes through `get_codegen_agent_config()`, which keeps a short in-process cache (so a model call does not hit the secrets
manager) that a save invalidates; other workers see a change within CACHE_SECONDS.

There is no silent fallback: if OpenAI/Anthropic is selected, every completion goes to it or fails with a clear reason — an
unreadable setting or a rejected key never quietly sends the prompt to a different model.
"""
from __future__ import annotations

import hashlib
import time
from typing import Any

import httpx
import structlog

from app.core.config import get_settings

log = structlog.get_logger()

PROVIDER_CODEGEN = "codegen"
PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_OPENAI = "openai"
VALID_PROVIDERS = {PROVIDER_CODEGEN, PROVIDER_ANTHROPIC, PROVIDER_OPENAI}
EXTERNAL_PROVIDERS = {PROVIDER_ANTHROPIC, PROVIDER_OPENAI}

CONFIG_SECRET_PATH = "workspace/codegen_agent_config"
# A key that older code wrote here (plain text) must not linger: it is removed on every save and on startup of a read.
LEGACY_REDIS_KEY = "workspace:codegen_agent_config"
CACHE_SECONDS = 10.0
VERIFY_CACHE_SECONDS = 60.0

LABELS = {PROVIDER_CODEGEN: "Codegen Agent (default)", PROVIDER_ANTHROPIC: "Anthropic key", PROVIDER_OPENAI: "OpenAI key"}
# The names the org's provider allowlist (codegen_governance) uses for each.
GOVERNANCE_NAMES = {PROVIDER_CODEGEN: "codegen_gateway", PROVIDER_ANTHROPIC: "anthropic", PROVIDER_OPENAI: "openai"}


class CodegenConfigError(RuntimeError):
    """The agent setting cannot be read (secrets backend down). Never answered by guessing a provider."""


_cache: tuple[float, dict[str, Any]] | None = None
_verify_cache: dict[str, tuple[float, tuple[bool, str]]] = {}


def mask_api_key(key: str | None) -> str | None:
    if not key:
        return None
    k = key.strip()
    if len(k) <= 8:
        return "****"
    return f"{k[:4]}••••••••{k[-4:]}"


def model_for(provider: str) -> str | None:
    s = get_settings()
    return {PROVIDER_OPENAI: s.codegen_openai_model, PROVIDER_ANTHROPIC: s.codegen_anthropic_model}.get(provider)


def available_providers() -> list[str]:
    """The agents an admin may choose, in display order. Without the self-hosted gateway (codegen_gateway_enabled=False) only
    the providers that take the admin's own key."""
    own_key = [PROVIDER_ANTHROPIC, PROVIDER_OPENAI]
    return ([PROVIDER_CODEGEN] if get_settings().codegen_gateway_enabled else []) + own_key


def default_provider() -> str:
    """The agent in use before an admin picks one: the gateway, or (without it) Anthropic awaiting a key."""
    return available_providers()[0]


def _default() -> dict[str, Any]:
    return {"provider": default_provider(), "api_key": None}


def reset_cache() -> None:
    global _cache
    _cache = None
    _verify_cache.clear()


async def get_codegen_agent_config() -> dict[str, Any]:
    """{"provider", "api_key"} — the default gateway when nothing was ever chosen. Raises CodegenConfigError if the setting
    cannot be read."""
    global _cache
    now = time.monotonic()
    if _cache and now - _cache[0] < CACHE_SECONDS:
        return dict(_cache[1])
    try:
        from app.storage.secrets import get_secrets_backend
        data = await get_secrets_backend().get(CONFIG_SECRET_PATH)
    except Exception as e:  # noqa: BLE001
        log.warning("codegen_agent_config_unreadable", error=type(e).__name__)
        raise CodegenConfigError("The codegen agent setting could not be read from the secrets store.") from None
    cfg = _default()
    # A saved choice that is no longer offered (the gateway option after codegen_gateway_enabled was turned off) reads as
    # the default rather than pointing every completion at a gateway that is not there.
    if data and data.get("provider") in available_providers():
        cfg = {"provider": data["provider"], "api_key": data.get("api_key") or None}
    _cache = (now, cfg)
    return dict(cfg)


async def save_codegen_agent_config(provider: str, api_key: str | None = None) -> dict[str, Any]:
    """Persist the choice in the secrets store (the key only for OpenAI/Anthropic; choosing the default removes any saved key)."""
    global _cache
    if provider not in available_providers():
        raise ValueError(f"Invalid provider: {provider}")
    key = api_key.strip() if (api_key and provider in EXTERNAL_PROVIDERS) else None
    data = {"provider": provider, "api_key": key, "updated_at": time.time()}
    from app.storage.secrets import get_secrets_backend
    await get_secrets_backend().put(CONFIG_SECRET_PATH, data)
    _cache = None
    _verify_cache.clear()
    await _purge_legacy_redis_copy()
    return {"provider": provider, "api_key": key}


async def _purge_legacy_redis_copy() -> None:
    try:
        from app.services.execution_router import get_redis
        await get_redis().delete(LEGACY_REDIS_KEY)
    except Exception:  # noqa: BLE001 — Redis being down is not a reason to fail a save
        log.debug("codegen_legacy_redis_purge_skipped")


async def verify_provider_key(provider: str, api_key: str | None) -> tuple[bool, str]:
    """Check the provider live: (ok, message). Never echoes the key. Results are cached for a minute per (provider, key)."""
    s = get_settings()
    clean = api_key.strip() if api_key else None
    if provider in EXTERNAL_PROVIDERS and not clean:
        return False, "Connection failed: API key is required"
    cache_key = f"{provider}:{hashlib.sha256((clean or '').encode()).hexdigest()}"
    hit = _verify_cache.get(cache_key)
    if hit and time.monotonic() - hit[0] < VERIFY_CACHE_SECONDS:
        return hit[1]
    result = await _verify(provider, clean, s)
    _verify_cache[cache_key] = (time.monotonic(), result)
    return result


async def _verify(provider: str, key: str | None, s) -> tuple[bool, str]:
    if provider == PROVIDER_CODEGEN:
        if not s.codegen_gateway_enabled:
            return False, "The built-in codegen agent is not available here: choose Anthropic or OpenAI and enter an API key"
        if not s.codegen_gateway_url:
            return False, "Connection failed: CODEGEN_GATEWAY_URL is not configured"
        headers = {"Authorization": f"Bearer {s.codegen_gateway_api_key}"} if s.codegen_gateway_api_key else {}
        url, label = f"{s.codegen_gateway_url.rstrip('/')}/models", "the model gateway"
    elif provider == PROVIDER_OPENAI:
        headers = {"Authorization": f"Bearer {key}"}
        url, label = f"{s.codegen_openai_base_url.rstrip('/')}/models", "OpenAI"
    elif provider == PROVIDER_ANTHROPIC:
        headers = {"x-api-key": key or "", "anthropic-version": "2023-06-01"}
        url, label = f"{s.codegen_anthropic_base_url.rstrip('/')}/models", "Anthropic"
    else:
        return False, f"Unsupported provider: {provider}"
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(url, headers=headers)
    except httpx.HTTPError as e:
        return False, f"Connection failed: could not reach {label} ({type(e).__name__})"
    if resp.status_code == 200:
        return True, "Successfully connected"
    if resp.status_code in (401, 403):
        return False, f"Connection failed: {label} rejected the API key" if provider in EXTERNAL_PROVIDERS else f"Connection failed (HTTP {resp.status_code})"
    if resp.status_code == 429:
        return False, f"Connection failed: {label} is rate limiting this key"
    return False, f"Connection failed (HTTP {resp.status_code})"
