"""Workspace -> Flink runner / DQ scoped service tokens (app/core/service_tokens.py)."""
from __future__ import annotations

import asyncio

import httpx
import pytest
from jose import jwt

from app.core import service_tokens as st
from app.core.config import get_settings

SECRET = "s3cret-for-tests"


def _decode(token, audience):
    return jwt.decode(token, SECRET, algorithms=["HS256"], audience=audience,
                      options={"require_exp": True, "require_iat": True, "require_jti": True})


@pytest.mark.parametrize("aud,scope", [(st.FLINK_AUDIENCE, st.FLINK_SCOPE_VALIDATE),
                                       (st.DQ_AUDIENCE, st.DQ_SCOPE_RULES_VALIDATE)])
def test_token_carries_exactly_what_the_callee_requires(aud, scope):
    c = _decode(st.mint_service_token(secret=SECRET, audience=aud, scope=scope), aud)
    assert c["iss"] == c["sub"] == "workspace"          # DQ allowlists iss "workspace"
    assert c["scope"].split() == [scope]                 # one operation only
    assert c["org_id"] == get_settings().org_id          # callees reject a missing/other org
    assert 0 < c["exp"] - c["iat"] <= 300                # callees cap lifetime at 300s


def test_every_call_gets_a_fresh_jti():
    a = _decode(st.mint_service_token(secret=SECRET, audience="x", scope="s"), "x")
    b = _decode(st.mint_service_token(secret=SECRET, audience="x", scope="s"), "x")
    assert a["jti"] != b["jti"]                          # DQ consumes each jti once


def _run(handler, env, **kw):
    s = get_settings()
    old = s.environment
    s.environment = env
    try:
        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
                return await st.post_with_service_token(c, "http://callee/v", json={}, **kw)
        return asyncio.run(go())
    finally:
        s.environment = old


def test_dev_falls_back_to_legacy_token_only_after_a_401():
    sent = []

    def handler(req):
        sent.append(req.headers["X-Internal-Service-Token"])
        return httpx.Response(401 if len(sent) == 1 else 200)

    r = _run(handler, "dev", signing_secret=SECRET, audience="a", scope="s", legacy_token="raw")
    assert r.status_code == 200 and sent[0] != "raw" and sent[1] == "raw"


def test_no_fallback_outside_dev():
    sent = []

    def handler(req):
        sent.append(req.headers["X-Internal-Service-Token"])
        return httpx.Response(401)

    r = _run(handler, "prod", signing_secret=SECRET, audience="a", scope="s", legacy_token="raw")
    assert r.status_code == 401 and sent and "raw" not in sent


def test_success_with_scoped_token_never_sends_legacy():
    sent = []

    def handler(req):
        sent.append(req.headers["X-Internal-Service-Token"])
        return httpx.Response(200)

    _run(handler, "dev", signing_secret=SECRET, audience="a", scope="s", legacy_token="raw")
    assert sent and "raw" not in sent


def test_missing_secret_outside_dev_never_sends_a_raw_token():
    def handler(req):
        raise AssertionError("no request may be sent")

    r = _run(handler, "prod", signing_secret=None, audience="a", scope="s", legacy_token="raw")
    assert r.status_code == 503
