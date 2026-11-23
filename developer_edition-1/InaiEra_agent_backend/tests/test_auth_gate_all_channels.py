"""
features.require_authenticated_user must hold on EVERY invocation
channel, not only POST /run.

The gap: the feature's own UI text promises "every run needs a real,
gateway-verified identity. A self-reported invoking_user is rejected,
not merely ignored." Only /run and /run/stream read the flag. batch-run,
MCP and webhooks never did, and invoked the agent regardless — verified
live: the same agent answered 401 on /run and 200 on /batch-run, and an
MCP tools/call executed the flow with no identity at all.

A credential proving the CALLER holds access to an agent (an MCP token,
a webhook token) is not the same claim as "this is who the person is",
which is exactly the distinction this feature exists to enforce.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi import HTTPException

from app.auth import enforce_authenticated_user


class FakeRequest:
    def __init__(self, **headers):
        self.headers = {k.lower().replace("_", "-"): v for k, v in headers.items()}


class FakeAgent:
    def __init__(self, features):
        self.features = features


GATED = {"require_authenticated_user": True}
OPEN = {"require_authenticated_user": False}


@pytest.mark.parametrize("channel", ["batch-run", "mcp", "webhook", "run"])
def test_every_channel_refuses_when_identity_cannot_be_proven(channel):
    with pytest.raises(HTTPException) as e:
        enforce_authenticated_user(FakeAgent(GATED), FakeRequest(), channel)
    assert e.value.status_code == 401
    # The channel is named so a refusal is traceable to where it happened.
    assert channel in str(e.value.detail)


@pytest.mark.parametrize("features", [OPEN, {}, None])
def test_an_agent_that_has_not_opted_in_is_untouched(features):
    """Returning None means "keep whatever invoking_user you had" — the
    flag must not start requiring identity for every agent."""
    assert enforce_authenticated_user(FakeAgent(features), FakeRequest(), "run") is None


def test_the_verified_identity_replaces_a_claimed_one(monkeypatch):
    monkeypatch.setenv("TRUST_ENVOY_HEADERS", "true")
    monkeypatch.setenv("ENVOY_INTERNAL_SECRET", "s3cret")
    r = FakeRequest(**{
        "x-inaiera-gateway-secret": "s3cret",
        "x-user-name": "alice",
        "x-user-roles": "some-realm-role",
        "x-tenant-id": "prod_metalake",
    })
    assert enforce_authenticated_user(FakeAgent(GATED), r, "batch-run") == "alice"


def test_a_wrong_secret_is_refused_not_downgraded(monkeypatch):
    monkeypatch.setenv("TRUST_ENVOY_HEADERS", "true")
    monkeypatch.setenv("ENVOY_INTERNAL_SECRET", "s3cret")
    r = FakeRequest(**{"x-inaiera-gateway-secret": "wrong", "x-user-name": "alice"})
    with pytest.raises(HTTPException) as e:
        enforce_authenticated_user(FakeAgent(GATED), r, "mcp")
    assert "did not match" in str(e.value.detail)


def test_the_refusal_explains_which_check_failed(monkeypatch):
    monkeypatch.setenv("TRUST_ENVOY_HEADERS", "false")
    with pytest.raises(HTTPException) as e:
        enforce_authenticated_user(FakeAgent(GATED), FakeRequest(), "webhook")
    assert "TRUST_ENVOY_HEADERS" in str(e.value.detail)


def test_all_four_channels_are_actually_wired_to_it():
    """The helper existing is not the fix; the call sites are. This reads
    the source so a channel cannot quietly stop enforcing."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent / "app" / "routers"
    for fname, channel in [("execution.py", "batch-run"), ("mcp.py", "mcp"),
                           ("webhooks.py", "webhook")]:
        src = (root / fname).read_text()
        assert "enforce_authenticated_user" in src, fname
        assert f'"{channel}"' in src, (fname, channel)
    # /run and /run/stream keep their original, equivalent enforcement.
    execution = (root / "execution.py").read_text()
    assert execution.count("require_verified_identity(request)") >= 2
