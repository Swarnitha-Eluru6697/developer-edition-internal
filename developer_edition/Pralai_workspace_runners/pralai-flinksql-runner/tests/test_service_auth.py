"""DQ-AUTH-012 — service-to-service calls (DQ -> /api/v1/validate,
this runner -> UCS /api/v1/quality/results) must use short-lived,
narrowly-scoped JWTs, not a standing, unscoped shared secret sent as-is.

Covers: mint_service_token (pure), require_service_scope's dual-accept
behavior (a migrated caller's scoped JWT AND an unmigrated caller's
legacy static token, per /api/v1/validate's two real callers), and the
real /api/v1/validate route end-to-end via the app's TestClient (client
fixture, tests/conftest.py).
"""
from __future__ import annotations

import time
from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from jose import jwt

from app.core.config import Settings
from app.core.service_auth import SERVICE_AUDIENCE, mint_service_token, require_service_scope


def _settings(**overrides):
    base = {"internal_service_token": "flink-secret", "org_id": "org-42"}
    base.update(overrides)
    return Settings(**base)


# ── mint_service_token (pure) ─────────────────────────────────────────────

def test_mint_produces_a_token_verifiable_with_the_same_signing_key():
    token = mint_service_token(
        signing_key="shared-secret", issuer="flinksql-runner", audience="ucs",
        scope="ucs.quality.checks.write", org_id="org1",
    )
    claims = jwt.decode(token, "shared-secret", algorithms=["HS256"], audience="ucs")
    assert claims["scope"] == "ucs.quality.checks.write"
    assert claims["org_id"] == "org1"


def test_token_expires_quickly():
    token = mint_service_token(signing_key="s", issuer="i", audience="a", scope="x")
    claims = jwt.decode(token, "s", algorithms=["HS256"], audience="a")
    assert claims["exp"] - claims["iat"] == 60


# ── require_service_scope dual-accept, via a throwaway app ────────────────

_app = FastAPI()


@_app.post("/validate")
async def _validate(_claims: Annotated[dict | None, Depends(require_service_scope("flink.jobs.validate"))]):
    return {"ok": True, "migrated": _claims is not None}


@pytest.fixture
def scope_client(monkeypatch):
    monkeypatch.setattr("app.core.service_auth.get_settings", lambda: _settings())
    return TestClient(_app)


def _token(**overrides):
    claims = {
        "iss": "dataquality", "aud": SERVICE_AUDIENCE, "scope": "flink.jobs.validate",
        "org_id": "org-42", "iat": int(time.time()), "exp": int(time.time()) + 60,
    }
    claims.update(overrides)
    return jwt.encode(claims, "flink-secret", algorithm="HS256")


def test_a_valid_scoped_jwt_from_a_migrated_caller_is_accepted(scope_client):
    r = scope_client.post("/validate", headers={"X-Internal-Service-Token": _token()})
    assert r.status_code == 200
    assert r.json()["migrated"] is True


def test_the_legacy_static_token_is_accepted_in_dev_by_default(scope_client):
    """Local stacks (ENVIRONMENT=dev) whose Workspace still sends the raw
    static token keep working."""
    r = scope_client.post("/validate", headers={"X-Internal-Service-Token": "flink-secret"})
    assert r.status_code == 200
    assert r.json()["migrated"] is False


def test_the_legacy_static_token_is_rejected_outside_dev_by_default(monkeypatch):
    """RUN-FLINK-009: the unscoped static-token fallback is OFF in
    staging/prod unless explicitly enabled."""
    monkeypatch.setattr("app.core.service_auth.get_settings",
                        lambda: _settings(environment="prod"))
    r = TestClient(_app).post("/validate", headers={"X-Internal-Service-Token": "flink-secret"})
    assert r.status_code == 401


def test_the_legacy_static_token_can_be_disabled_in_dev(monkeypatch):
    monkeypatch.setattr("app.core.service_auth.get_settings",
                        lambda: _settings(legacy_internal_token_enabled=False))
    r = TestClient(_app).post("/validate", headers={"X-Internal-Service-Token": "flink-secret"})
    assert r.status_code == 401


def test_the_legacy_static_token_only_when_explicitly_enabled_outside_dev(monkeypatch):
    monkeypatch.setattr("app.core.service_auth.get_settings",
                        lambda: _settings(environment="prod", legacy_internal_token_enabled=True))
    r = TestClient(_app).post("/validate", headers={"X-Internal-Service-Token": "flink-secret"})
    assert r.status_code == 200


def test_a_service_jwt_with_no_org_claim_is_rejected(scope_client):
    """RUN-FLINK-009: the org comparison used to be skipped entirely when
    the claim was absent."""
    r = scope_client.post("/validate", headers={"X-Internal-Service-Token": _token(org_id=None)})
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "ORG_REQUIRED"


def test_a_service_jwt_with_an_empty_org_claim_is_rejected(scope_client):
    r = scope_client.post("/validate", headers={"X-Internal-Service-Token": _token(org_id="")})
    assert r.status_code == 403


def test_no_token_is_rejected(scope_client):
    r = scope_client.post("/validate")
    assert r.status_code == 401


def test_garbage_that_is_neither_a_valid_jwt_nor_the_legacy_secret_is_rejected(scope_client):
    r = scope_client.post("/validate", headers={"X-Internal-Service-Token": "garbage"})
    assert r.status_code == 401


def test_a_jwt_with_the_wrong_scope_is_rejected(scope_client):
    r = scope_client.post("/validate",
                          headers={"X-Internal-Service-Token": _token(scope="ucs.quality.checks.write")})
    assert r.status_code == 403


def test_a_jwt_for_the_wrong_audience_is_rejected(scope_client):
    r = scope_client.post("/validate", headers={"X-Internal-Service-Token": _token(aud="ucs")})
    assert r.status_code == 401


def test_an_expired_jwt_is_rejected(scope_client):
    expired = _token(iat=int(time.time()) - 120, exp=int(time.time()) - 60)
    r = scope_client.post("/validate", headers={"X-Internal-Service-Token": expired})
    assert r.status_code == 401


def test_a_jwt_bound_to_a_different_org_is_rejected(scope_client):
    r = scope_client.post("/validate", headers={"X-Internal-Service-Token": _token(org_id="other-org")})
    assert r.status_code == 403


# ── The real /api/v1/validate route accepts a scoped JWT end-to-end ──────

def test_real_validate_route_accepts_a_migrated_callers_scoped_jwt(client, monkeypatch):
    from app.core.config import get_settings as real_get_settings
    settings = real_get_settings()
    token = jwt.encode(
        {"iss": "dataquality", "aud": SERVICE_AUDIENCE, "scope": "flink.jobs.validate",
         "org_id": settings.org_id, "iat": int(time.time()), "exp": int(time.time()) + 60},
        settings.internal_service_token, algorithm="HS256",
    )
    resp = client.post("/api/v1/validate", headers={"X-Internal-Service-Token": token},
                       json={"sql": "SELECT 1"})
    assert resp.status_code != 401 and resp.status_code != 403


def test_real_validate_route_still_accepts_the_legacy_static_token_in_dev(client, internal_headers):
    """The test app runs with ENVIRONMENT=dev (legacy fallback on by default there)."""
    resp = client.post("/api/v1/validate", headers=internal_headers, json={"sql": "SELECT 1"})
    assert resp.status_code != 401 and resp.status_code != 403


# ── _post_to_ucs mints a scoped token instead of sending the raw secret ──

async def test_post_to_ucs_sends_a_minted_token_not_the_raw_shared_secret():
    from app.services.quality_consumer import _post_to_ucs

    captured = {}

    class _FakeResp:
        status_code = 200

    class _FakeClient:
        async def post(self, url, json=None, headers=None, timeout=None):
            captured["headers"] = headers
            return _FakeResp()

    settings = _settings(ucs_internal_token="ucs-secret", ucs_url="http://ucs.internal",
                         quality_consumer_retry_backoff_seconds=1)
    ok = await _post_to_ucs(_FakeClient(), settings, {"name": "r", "fqn": "f"})

    assert ok == 200          # returns the final HTTP status
    auth = captured["headers"]["Authorization"]
    assert auth.startswith("Bearer ")
    token = auth.removeprefix("Bearer ")
    assert token != "ucs-secret"  # the raw shared secret must never be sent as-is
    claims = jwt.decode(token, "ucs-secret", algorithms=["HS256"], audience="ucs")
    assert claims["scope"] == "ucs.quality.checks.write"
    assert claims["org_id"] == settings.org_id


def test_real_validate_route_rejects_a_service_jwt_without_org(client):
    from app.core.config import get_settings as real_get_settings
    settings = real_get_settings()
    token = jwt.encode(
        {"iss": "dataquality", "aud": SERVICE_AUDIENCE, "scope": "flink.jobs.validate",
         "iat": int(time.time()), "exp": int(time.time()) + 60},
        settings.internal_service_token, algorithm="HS256",
    )
    resp = client.post("/api/v1/validate", headers={"X-Internal-Service-Token": token},
                       json={"sql": "SELECT 1"})
    assert resp.status_code == 403


def test_post_to_ucs_sends_the_idempotency_key():
    import asyncio
    from app.services.quality_consumer import _post_to_ucs

    captured = {}

    class _FakeResp:
        status_code = 201

    class _FakeClient:
        async def post(self, url, json=None, headers=None, timeout=None):
            captured.update(headers)
            return _FakeResp()

    settings = _settings(ucs_internal_token="ucs-secret", ucs_url="http://ucs.internal")
    ok = asyncio.run(_post_to_ucs(_FakeClient(), settings, {"name": "r", "fqn": "f"},
                                  idempotency_key="quality.violations-0-42"))
    assert ok and captured["Idempotency-Key"] == "quality.violations-0-42"
