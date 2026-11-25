"""
Real, shared pytest configuration for the whole backend test suite.

ALLOW_MOCK_MODELS (section 0.58, GAP_REPORT) - model_adapter.py's
call_llm now requires this real, explicit opt-in before returning a
mock response when no ANTHROPIC_API_KEY is configured; without it, a
missing key genuinely raises LLMCallError, matching real production
behavior. This test suite has no real API key configured (by design -
see the project's own README), and the overwhelming majority of these
tests are about real, non-LLM-provider-specific behavior (routing,
persistence, node execution, isolation, etc.) that should keep working
against the same honest mock output it always has. Setting this once,
here, for the whole real session is what keeps that true - an
individual test that specifically wants to exercise the "no mocks,
real failure" path (see test_llm_error_handling.py) uses monkeypatch
to override it for just that one test, which real pytest semantics
already isolate correctly from every other test.
"""
import os

os.environ.setdefault("ALLOW_MOCK_MODELS", "true")
# A real, fixed test key (section 0.59, GAP_REPORT) - avoids the real,
# loud warning secrets_crypto.py logs when none is set, and keeps
# encryption/decryption deterministic across test runs within the same
# session rather than a fresh ephemeral key per process. Not a real
# production secret - this repo's own test suite has no real data to
# protect long-term.
os.environ.setdefault("SECRETS_ENCRYPTION_KEY", "aeER5wskKgtIDSnKqQpPXDDD5GfjPqAV2CSDpegqhSE=")


# Deny by default (docs/auth/01-CONTRACT.md): app/main.py's protect_app
# refuses every non-public route without a gateway-verified principal, and
# each route needs a permission. Most of this suite tests behaviour BEHIND
# that boundary, so every TestClient arrives as Envoy would deliver a user
# holding every permission this app declares (permissions.json). A test of
# the boundary itself passes its own `headers=` (e.g. `headers={}` for a
# bare client) — see tests/test_permission_auth.py.
import base64 as _b64
import json as _json
from pathlib import Path as _Path

GATEWAY_TEST_SECRET = "test-gateway-secret"
os.environ.setdefault("TRUST_ENVOY_HEADERS", "true")
os.environ.setdefault("ENVOY_INTERNAL_SECRET", GATEWAY_TEST_SECRET)

ALL_PERMISSIONS = [p["id"] for p in _json.loads(
    (_Path(__file__).resolve().parent.parent / "permissions.json").read_text())["permissions"]]


def gateway_headers(permissions=None, user: str = "test-user", secret: str = GATEWAY_TEST_SECRET,
                    **extra) -> dict:
    """Headers exactly as Envoy sets them (list headers base64 JSON)."""
    perms = ALL_PERMISSIONS if permissions is None else list(permissions)
    h = {
        "x-inaiera-gateway-secret": secret,
        "x-user-name": user,
        "x-user-id": f"id-{user}",
        "x-tenant-id": "prod_metalake",
        "x-user-permissions": _b64.b64encode(_json.dumps(perms).encode()).decode(),
    }
    h.update(extra)
    return h


from starlette.testclient import TestClient as _StarletteTestClient  # noqa: E402

_original_testclient_init = _StarletteTestClient.__init__


def _gateway_testclient_init(self, *args, **kwargs):
    if "headers" not in kwargs:
        kwargs["headers"] = gateway_headers()
    _original_testclient_init(self, *args, **kwargs)


_StarletteTestClient.__init__ = _gateway_testclient_init


import pytest


@pytest.fixture()
def no_stored_provider_key():
    """Blank a provider's STORED key for one test, then put it back.

    The comment at the top of this file says the suite has no real API
    key configured. That stopped being true the moment someone saved one
    through the Integrations page — and several tests here assume the
    "no key" path by calling `monkeypatch.delenv("ANTHROPIC_API_KEY")`,
    which removes only the environment variable. The adapter falls back
    to the stored provider config, so a real saved key silently defeats
    that setup.

    Those tests used to pass only because a fixture in
    test_model_provider_config.py wiped the row permanently — an
    accidental dependency between files that also destroyed the
    operator's key on every run. With that fixed, this is how a test
    asks for the no-key path honestly.

    Usage: `def test_x(self, monkeypatch, no_stored_provider_key):`
    then `no_stored_provider_key("Anthropic")`.
    """
    from sqlmodel import Session

    from app.db import engine
    from app.models import ModelProvider

    saved: dict[str, tuple] = {}

    def _clear(provider: str):
        with Session(engine) as s:
            mp = s.get(ModelProvider, provider)
            if mp is None:
                return
            saved[provider] = (mp.api_key, mp.api_key_configured, mp.status)
            mp.api_key, mp.api_key_configured, mp.status = None, False, "needs_key"
            s.add(mp)
            s.commit()

    yield _clear

    # finally-equivalent: a failing test must not take the key with it.
    for provider, (key, configured, status) in saved.items():
        with Session(engine) as s:
            mp = s.get(ModelProvider, provider)
            mp.api_key, mp.api_key_configured, mp.status = key, configured, status
            s.add(mp)
            s.commit()
