"""W-002: /api/v1/dq/validate/rule reaches DQ with a short-lived scoped service JWT.

The DQ service plays itself here (an httpx MockTransport standing in for
/api/v1/render/validate/rule): it decodes whatever X-Internal-Service-Token the
Workspace sends, exactly as DQ's inbound check would — audience "dataquality",
scope "dq.rules.validate", this org, issuer "workspace", lifetime <= 300s.
"""
from __future__ import annotations

import httpx
import pytest
from jose import jwt

from app.api import dq_proxy
from app.api.settings import dq_missing_settings
from app.core.config import get_settings

SIGNING_SECRET = "dq-inbound-service-jwt-secret"
RULE = {"rule_type": "not_null", "dimension": "completeness", "column_name": "id"}


@pytest.fixture
def dq_calls(monkeypatch):
    """Point dq_proxy at a fake DQ service; yields the list of requests it received."""
    calls: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return httpx.Response(200, json={"valid": True, "errors": []})

    real = httpx.AsyncClient
    monkeypatch.setattr(dq_proxy.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    return calls


def _configure(monkeypatch, *, environment, signing_secret, legacy_token=None):
    s = get_settings()
    monkeypatch.setattr(s, "environment", environment)
    monkeypatch.setattr(s, "dq_service_jwt_secret", signing_secret)
    monkeypatch.setattr(s, "dq_internal_token", legacy_token)
    monkeypatch.setattr(s, "dq_service_url", "http://dq.test")


def test_validate_rule_sends_a_scoped_short_lived_service_jwt_in_production(
        client, gateway_headers, monkeypatch, dq_calls):
    _configure(monkeypatch, environment="prod", signing_secret=SIGNING_SECRET)

    r = client.post("/api/v1/dq/validate/rule", json=RULE, headers=gateway_headers(roles=["data_engineer"]))

    assert r.status_code == 200, r.text
    (req,) = dq_calls
    assert str(req.url) == "http://dq.test/api/v1/render/validate/rule"
    claims = jwt.decode(req.headers["X-Internal-Service-Token"], SIGNING_SECRET, algorithms=["HS256"],
                        audience="dataquality",
                        options={"require_exp": True, "require_iat": True, "require_jti": True})
    assert claims["iss"] == claims["sub"] == "workspace"
    assert claims["scope"].split() == ["dq.rules.validate"]
    assert claims["org_id"] == get_settings().org_id
    assert 0 < claims["exp"] - claims["iat"] <= 300


def test_validate_rule_fails_closed_in_production_without_the_signing_secret(
        client, gateway_headers, monkeypatch, dq_calls):
    """The W-002 failure: only the legacy raw token is present (all the chart used to mount)."""
    _configure(monkeypatch, environment="prod", signing_secret=None, legacy_token="legacy-raw-token")

    r = client.post("/api/v1/dq/validate/rule", json=RULE, headers=gateway_headers(roles=["data_engineer"]))

    assert r.status_code == 503
    assert dq_calls == [], "no request, and above all no raw token, may reach DQ"


# Integration health: the signing secret is what production needs, not the legacy token.
def _settings(monkeypatch, *, environment, signing_secret=None, legacy_token=None, url="http://dq.test"):
    s = get_settings()
    monkeypatch.setattr(s, "environment", environment)
    monkeypatch.setattr(s, "dq_service_jwt_secret", signing_secret)
    monkeypatch.setattr(s, "dq_internal_token", legacy_token)
    monkeypatch.setattr(s, "dq_service_url", url)
    return s


def test_health_reports_the_signing_secret_missing_in_production_even_with_the_legacy_token(monkeypatch):
    s = _settings(monkeypatch, environment="prod", legacy_token="legacy-raw-token")
    assert dq_missing_settings(s) == ["DQ_SERVICE_JWT_SECRET"]


def test_health_is_clean_in_production_with_the_signing_secret(monkeypatch):
    s = _settings(monkeypatch, environment="prod", signing_secret=SIGNING_SECRET)
    assert dq_missing_settings(s) == []


def test_health_accepts_either_credential_in_dev(monkeypatch):
    assert dq_missing_settings(_settings(monkeypatch, environment="dev", legacy_token="raw")) == []
    assert dq_missing_settings(_settings(monkeypatch, environment="dev", signing_secret=SIGNING_SECRET)) == []
    assert dq_missing_settings(_settings(monkeypatch, environment="dev")) == ["DQ_SERVICE_JWT_SECRET"]


def test_health_still_reports_a_missing_url(monkeypatch):
    s = _settings(monkeypatch, environment="prod", signing_secret=SIGNING_SECRET, url="")
    assert dq_missing_settings(s) == ["DQ_SERVICE_URL"]
