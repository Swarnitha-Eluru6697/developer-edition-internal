"""
Verified identity for agents with features.require_authenticated_user.

The only identity source is the gateway contract (x-user-name proven by
x-inaiera-gateway-secret). The former legacy OIDC path (X-Auth-Request-Email,
OIDC_GATEWAY_SHARED_SECRET, OIDC_DEV_BYPASS_*) is removed: these tests hold
that it is no longer honoured anywhere, and that the gateway identity still
replaces whatever invoking_user a body claims.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from app.main import app
from conftest import gateway_headers

client = TestClient(app)


def create_agent(**overrides):
    body = {"name": "Auth Test Agent", "subtype": "Custom", "app_type": "workflow"}
    body.update(overrides)
    r = client.post("/api/agents", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def require_auth(agent_id):
    r = client.patch(f"/api/agents/{agent_id}/features", json={"features": {"require_authenticated_user": True}})
    assert r.status_code == 200, r.text


class FakeRequest:
    def __init__(self, headers):
        self.headers = headers


class TestHeaderExtraction:
    def test_no_header_present_returns_none(self):
        from app.auth import get_verified_identity
        assert get_verified_identity(FakeRequest({})) is None

    def test_the_legacy_header_is_ignored(self, monkeypatch):
        from app.auth import get_verified_identity
        monkeypatch.setenv("OIDC_GATEWAY_SHARED_SECRET", "s")
        r = FakeRequest({"X-Auth-Request-Email": "real.person@company.com",
                         "X-Gateway-Shared-Secret": "s"})
        assert get_verified_identity(r) is None

    def test_the_dev_bypass_is_gone(self, monkeypatch):
        from app.auth import get_verified_identity
        monkeypatch.setenv("OIDC_DEV_BYPASS_ENABLED", "true")
        monkeypatch.setenv("OIDC_DEV_BYPASS_USER", "dev@local")
        assert get_verified_identity(FakeRequest({})) is None

    def test_the_gateway_identity_is_returned(self):
        from app.auth import get_verified_identity
        h = {k.lower(): v for k, v in gateway_headers(user="real.person").items()}
        assert get_verified_identity(FakeRequest(h)) == "real.person"


class TestEndToEnd:
    def test_a_run_with_no_auth_requirement_works(self):
        agent = create_agent()
        r = client.post(f"/api/agents/{agent['id']}/run", json={"input": "hello", "invoking_user": "user:anyone"})
        assert r.status_code == 200

    def test_a_run_through_the_gateway_succeeds_when_auth_is_required(self):
        agent = create_agent()
        require_auth(agent["id"])
        r = client.post(f"/api/agents/{agent['id']}/run", json={"input": "hello"})
        assert r.status_code == 200, r.text

    def test_the_gateway_identity_overrides_a_spoofed_body_claim(self):
        agent = create_agent()
        require_auth(agent["id"])
        r = client.post(
            f"/api/agents/{agent['id']}/run",
            json={"input": "hello", "invoking_user": "user:spoofed-claim"},
            headers=gateway_headers(user="real.verified"),
        )
        assert r.status_code == 200, r.text
        trace_id = r.json()["trace_id"]
        tr = client.get("/api/traces").json()
        mine = next(t for t in tr if t["id"] == trace_id)
        assert "real.verified" in mine["principal"]
        assert "spoofed-claim" not in mine["principal"]

    def test_a_legacy_header_without_the_gateway_is_refused(self):
        bare = TestClient(app, headers={})
        agent = create_agent()
        r = bare.post(f"/api/agents/{agent['id']}/run", json={"input": "hello"},
                      headers={"X-Auth-Request-Email": "attacker@evil.com"})
        assert r.status_code == 401
