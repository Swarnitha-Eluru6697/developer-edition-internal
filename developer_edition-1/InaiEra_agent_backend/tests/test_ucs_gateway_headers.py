"""
Outbound calls to UCS speak the same five-header gateway contract this
service accepts on the way in.

UCS adopts the same shared library (InaiEra_dataplane_GatewayAuth), so a
call from here should present the whole contract rather than a subset.
It used to send three headers — name, groups, tenant — omitting both
`x-user-roles` and `x-inaiera-gateway-secret`.

Verified live against UCS on :5178 while writing this: 401 with no
headers, 200 with those three, and 200 for a WRONG secret as readily as
the right one. So UCS is not verifying the secret today; sending it
prepares for when it does.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app import ucs_client


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("ENVOY_INTERNAL_SECRET", raising=False)
    monkeypatch.delenv("UCS_GATEWAY_SECRET", raising=False)


def test_the_three_original_headers_are_still_sent():
    h = ucs_client.gateway_headers("user:padmin", ["data-engineers"])
    assert h["X-User-Name"] == "user:padmin"
    assert h["X-User-Groups"] == "data-engineers"
    assert h["X-Tenant-Id"] == ucs_client.UCS_TENANT


def test_groups_are_comma_joined_the_way_ucs_splits_them():
    h = ucs_client.gateway_headers("user:a", ["g1", "g2", "g3"])
    assert h["X-User-Groups"] == "g1,g2,g3"


def test_no_groups_sends_an_empty_string_not_a_missing_header():
    """UCS decides against caller-supplied groups; a missing header and
    an empty one are not the same input to it."""
    h = ucs_client.gateway_headers("user:a", None)
    assert h["X-User-Groups"] == ""


def test_the_secret_is_sent_when_configured(monkeypatch):
    monkeypatch.setenv("ENVOY_INTERNAL_SECRET", "s3cret")
    h = ucs_client.gateway_headers("user:a", ["g"])
    assert h["X-Inaiera-Gateway-Secret"] == "s3cret"


def test_the_secret_header_is_absent_rather_than_empty_when_unconfigured():
    """An empty secret would fail UCS's check the moment it turns one
    on, converting calls that work today into 401s. Absent is honest;
    empty is a trap."""
    h = ucs_client.gateway_headers("user:a", ["g"])
    assert "X-Inaiera-Gateway-Secret" not in h


def test_roles_are_omitted_when_unknown():
    """This service resolves roles against its OWN vocabulary. Handing
    UCS one of those to resolve against a different product's would be
    asserting something untrue, so absent lets UCS apply its default."""
    h = ucs_client.gateway_headers("user:a", ["g"])
    assert "X-User-Roles" not in h


def test_roles_are_forwarded_when_known():
    h = ucs_client.gateway_headers("user:a", ["g"], ["platform-admin", "builder"])
    assert h["X-User-Roles"] == "platform-admin,builder"


def test_authz_check_uses_the_shared_builder(monkeypatch):
    """The helper existing is not the fix — the call site is."""
    seen = {}

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"allowed": True}

    def fake_post(url, json=None, headers=None, timeout=None):
        seen["url"] = url
        seen["headers"] = headers
        seen["json"] = json
        return FakeResp()

    monkeypatch.setenv("ENVOY_INTERNAL_SECRET", "s3cret")
    monkeypatch.setattr(ucs_client.httpx, "post", fake_post)
    ucs_client._authz_check("user:padmin", ["data-engineers"], "read",
                            "pg_sales.public.orders", roles=["platform-admin"])

    assert seen["url"].endswith("/api/v1/authz/check")
    for header in ("X-User-Name", "X-User-Groups", "X-Tenant-Id",
                   "X-Inaiera-Gateway-Secret", "X-User-Roles"):
        assert header in seen["headers"], header
    # The body still carries the decision inputs — UCS decides against
    # the groups it is given, not against membership it looks up.
    assert seen["json"]["groups"] == ["data-engineers"]


def test_ucs_base_url_default_points_at_the_live_port():
    """UCS moved from 5174 to 5178; a stale default silently makes every
    entitlement call unreachable."""
    assert "5174" not in ucs_client.UCS_BASE_URL
