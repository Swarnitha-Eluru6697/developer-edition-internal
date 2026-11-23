"""Gate 1 (InaiEra Gateway Auth) — identity now arrives as Envoy-injected,
trusted headers, verified via gateway_auth's core primitives, instead of a
JWT this service validates itself against JWKS.

Verifies:
 - A request with no headers at all returns 401
 - A request with the wrong (or no) gateway secret returns 401
 - A request missing the one mandatory identity header (X-User-Name)
   returns 401
 - TRUST_ENVOY_HEADERS not being the literal string "true" returns 401,
   regardless of how correct everything else is
 - A fully valid request reaches app.core.auth.Principal construction
   (RBAC/business logic is exercised separately — tests/test_rbac.py)
"""
from __future__ import annotations

import pytest
from conftest import subject_for


def test_healthz_does_not_require_auth(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_me_requires_auth(client):
    r = client.get("/api/v1/auth/me")
    assert r.status_code == 401


@pytest.mark.integration
def test_me_with_valid_gateway_headers(client, auth_headers):
    """Hits the DB to upsert a user row on first sight."""
    r = client.get("/api/v1/auth/me", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["claims"]["user_id"] == subject_for("test-user")      # W-020: the subject, not the username
    assert body["claims"]["username"] == "test-user"
    assert body["claims"]["org_id"] == "00000000-0000-0000-0000-000000000001"
    assert "roles" not in body["claims"] and "rbac" not in body
    assert "workspace:access:view" in body["permissions"]
    assert body["user"]["external_id"] == subject_for("test-user")


def test_missing_gateway_secret_rejected(client, gateway_headers):
    headers = gateway_headers(secret=None)
    r = client.get("/api/v1/auth/me", headers=headers)
    assert r.status_code == 401
    assert r.json()["detail"] == "request did not come through the gateway"


def test_wrong_gateway_secret_rejected(client, gateway_headers):
    headers = gateway_headers(secret="not-the-real-secret")
    r = client.get("/api/v1/auth/me", headers=headers)
    assert r.status_code == 401
    assert r.json()["detail"] == "request did not come through the gateway"


def test_missing_user_name_header_rejected(client, gateway_headers):
    """X-User-Name is the only mandatory identity header — a request that
    came through the gate but carries no username still can't be resolved
    to anyone."""
    headers = gateway_headers(user_name=None)
    r = client.get("/api/v1/auth/me", headers=headers)
    assert r.status_code == 401
    assert r.json()["detail"] == "missing identity headers"


def test_trust_envoy_headers_disabled_rejects_even_a_fully_correct_request(client, gateway_headers, monkeypatch):
    """The single most common local/deploy misconfiguration per the
    gateway_auth contract: TRUST_ENVOY_HEADERS must be the literal string
    "true" — anything else (unset, "True", "1") fails closed."""
    monkeypatch.delenv("TRUST_ENVOY_HEADERS", raising=False)
    headers = gateway_headers()
    r = client.get("/api/v1/auth/me", headers=headers)
    assert r.status_code == 401
    assert "TRUST_ENVOY_HEADERS" in r.json()["detail"]


def test_trust_envoy_headers_must_be_the_literal_string_true(client, gateway_headers, monkeypatch):
    monkeypatch.setenv("TRUST_ENVOY_HEADERS", "True")  # not the literal lowercase "true"
    headers = gateway_headers()
    r = client.get("/api/v1/auth/me", headers=headers)
    assert r.status_code == 401
    assert "TRUST_ENVOY_HEADERS" in r.json()["detail"]
