"""
UCS admin calls are made as a service identity (gateway secret + principal +
permissions), the way AI/ML Studio does — UCS no longer accepts bearer tokens.
"""
import base64
import json
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app import ucs_client, ucs_sync


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in ("UCS_GATEWAY_SECRET", "ENVOY_INTERNAL_SECRET"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(ucs_sync, "UCS_ADMIN_TOKEN", "")
    monkeypatch.setattr(ucs_sync, "UCS_ADMIN_USERNAME", "")
    monkeypatch.setattr(ucs_sync, "UCS_ADMIN_PASSWORD", "")
    monkeypatch.setattr(ucs_sync, "_minted_token", None)


def _perms(headers):
    return json.loads(base64.b64decode(headers["X-User-Permissions"]))


def test_admin_headers_are_the_service_identity_with_no_bearer(monkeypatch):
    monkeypatch.setenv("UCS_GATEWAY_SECRET", "gw")
    h = ucs_sync._admin_headers()
    assert "Authorization" not in h
    assert h["X-Inaiera-Gateway-Secret"] == "gw"
    assert h["X-User-Name"] == ucs_sync.UCS_SERVICE_USERNAME == "agents-service"
    assert h["X-User-Id"] == "agents-service"
    assert h["X-Tenant-Id"] == ucs_client.UCS_TENANT
    assert _perms(h) == ["ucs:catalogs:manage", "ucs:rbac:manage"]


def test_envoy_secret_is_used_when_ucs_gateway_secret_is_unset(monkeypatch):
    monkeypatch.setenv("ENVOY_INTERNAL_SECRET", "envoy")
    assert ucs_sync._admin_headers()["X-Inaiera-Gateway-Secret"] == "envoy"


def test_no_token_and_no_login_are_needed(monkeypatch):
    """With only the gateway secret set, a provisioning call goes out — no IdP is contacted."""
    monkeypatch.setenv("UCS_GATEWAY_SECRET", "gw")
    ok = MagicMock(status_code=200, json=lambda: {"ok": True})
    with patch("app.ucs_sync.httpx.post", return_value=ok) as post:
        ucs_sync._admin_post("/api/v1/admin/schemas", {"catalog": "c", "name": "s"})
    assert post.call_count == 1
    url = post.call_args.args[0]
    assert url.endswith("/api/v1/admin/schemas")
    assert "Authorization" not in post.call_args.kwargs["headers"]


def test_a_401_is_not_retried_with_a_login(monkeypatch):
    monkeypatch.setenv("UCS_GATEWAY_SECRET", "gw")
    monkeypatch.setattr(ucs_sync, "UCS_ADMIN_USERNAME", "u")
    monkeypatch.setattr(ucs_sync, "UCS_ADMIN_PASSWORD", "p")
    denied = MagicMock(status_code=401, text="no")
    with patch("app.ucs_sync.httpx.post", return_value=denied) as post, \
         patch("app.ucs_sync.mint_admin_token") as mint:
        with pytest.raises(ucs_sync.UCSSyncError):
            ucs_sync._admin_post("/api/v1/admin/schemas", {})
    assert post.call_count == 1
    mint.assert_not_called()


def test_nothing_configured_is_a_clear_error():
    with pytest.raises(ucs_sync.UCSSyncError, match="UCS_GATEWAY_SECRET"):
        ucs_sync._admin_headers()


def test_bearer_token_remains_a_dev_fallback(monkeypatch):
    monkeypatch.setattr(ucs_sync, "UCS_ADMIN_TOKEN", "dev-token")
    assert ucs_sync._admin_headers()["Authorization"] == "Bearer dev-token"


def test_lineage_carries_the_producer_permission(monkeypatch):
    monkeypatch.setenv("UCS_GATEWAY_SECRET", "gw")
    ok = MagicMock(status_code=200, json=lambda: {})
    with patch("app.ucs_client.httpx.post", return_value=ok) as post:
        ucs_client.emit_lineage_event("job", "run-1", "COMPLETE")
    h = post.call_args.kwargs["headers"]
    assert _perms(h) == ["ucs:lineage-events:create"]
    assert h["X-Inaiera-Gateway-Secret"] == "gw"
