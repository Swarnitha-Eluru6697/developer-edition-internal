"""W-003: POST /auth/exchange only stores a bearer token it has itself verified.

CurrentUser proves who the *gateway* says the caller is; it says nothing about
the Authorization bearer that exchange then writes into the session cookie
(the session's credential; since X-001 no service receives it). So exchange must
validate that exact token against the control-plane JWKS (signature, issuer,
audience, expiry, org) and require its sub / org_id to equal the trusted gateway
identity. Anything else is rejected and no cookie is set.
"""
from __future__ import annotations

import time
import uuid
from types import SimpleNamespace

import pytest
from conftest import TEST_ORG_ID, _headers, subject_for
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jose import jwk, jwt

import app.core.auth as core_auth
from app.core.config import get_settings
from app.core.deps import get_db
from app.main import app
from app.services import audit_service
from app.services.workspace_service import WorkspaceService

KID = "test-key-1"
USER = "test-user"        # the gateway username _headers() presents (X-User-Name)
SUBJECT = subject_for(USER)   # ... and the immutable subject it presents (X-User-Id): the identity (W-020)


def _rsa_pem():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()


SIGNING_PEM = _rsa_pem()          # the control plane's key
OTHER_PEM = _rsa_pem()            # anyone else's key


def _jwks_entry():
    public = jwk.construct(SIGNING_PEM, "RS256").public_key().to_dict()
    return {**public, "kid": KID, "alg": "RS256", "use": "sig"}


def make_token(*, sub=SUBJECT, org_id=TEST_ORG_ID, aud=None, iss=None, ttl=900, pem=None, kid=KID, extra=None):
    s = get_settings()
    now = int(time.time())
    claims = {"sub": sub, "org_id": org_id, "roles": ["developer"], "iat": now, "exp": now + ttl,
              "aud": aud or s.control_plane_audience, "iss": iss or s.control_plane_issuer, **(extra or {})}
    return jwt.encode(claims, pem or SIGNING_PEM, algorithm="RS256", headers={"kid": kid})


@pytest.fixture
def env(monkeypatch):
    # The control plane's JWKS, primed so nothing goes to the network.
    cache = core_auth._jwks_cache
    monkeypatch.setattr(cache, "_keys", {KID: _jwks_entry()})
    monkeypatch.setattr(cache, "_fetched_at", time.time() + 3600)

    recorded = []

    async def _db():
        yield SimpleNamespace()

    async def _ensure_user(self, principal):
        return SimpleNamespace(id=uuid.uuid4(), external_id=principal.user_id)

    async def _audit(self, **kw):
        recorded.append(kw)

    app.dependency_overrides[get_db] = _db
    monkeypatch.setattr(WorkspaceService, "ensure_user", _ensure_user)
    monkeypatch.setattr(audit_service.AuditService, "record", _audit)
    yield TestClient(app), recorded
    app.dependency_overrides.clear()


def _exchange(client, token, **header_kw):
    return client.post("/api/v1/auth/exchange",
                       headers={**_headers(**header_kw), "Authorization": f"Bearer {token}"})


def test_matching_token_is_exchanged_for_an_httponly_strict_cookie(env):
    client, recorded = env
    token = make_token(ttl=600)
    r = _exchange(client, token)
    assert r.status_code == 204, r.text
    cookie = r.headers["set-cookie"]
    assert f"workspace_session={token}" in cookie
    assert "httponly" in cookie.lower() and "samesite=strict" in cookie.lower()
    max_age = int(next(p.split("=")[1] for p in cookie.split(";") if p.strip().lower().startswith("max-age")))
    assert 540 <= max_age <= 600                      # driven by the verified exp
    assert len(recorded) == 1


def test_a_valid_token_for_a_different_user_than_the_gateway_identity_is_rejected(env):
    """The W-003 scenario: trusted gateway identity A, forwarded (valid!) bearer for B."""
    client, recorded = env
    r = _exchange(client, make_token(sub="someone-else"))
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "TOKEN_IDENTITY_MISMATCH"
    assert "set-cookie" not in r.headers
    assert recorded == [], "a rejected exchange must not be recorded as a session exchange"


def test_a_token_for_another_org_is_rejected(env):
    client, _ = env
    r = _exchange(client, make_token(org_id=str(uuid.uuid4())))
    assert r.status_code == 403
    assert "set-cookie" not in r.headers


@pytest.mark.parametrize("make", [
    lambda: "not-a-jwt",
    lambda: "a.b.c",
    lambda: make_token(aud="some-other-service"),                       # wrong audience
    lambda: make_token(iss="https://evil.example"),                     # wrong issuer
    lambda: make_token(ttl=-60),                                        # expired
    lambda: make_token(pem=OTHER_PEM),                                  # signed by someone else
    lambda: make_token(kid="unknown-kid"),                              # key the control plane never published
    lambda: jwt.encode({"sub": USER, "org_id": TEST_ORG_ID, "exp": 4102444800}, "k", algorithm="HS256"),  # unsigned-by-CP
], ids=["malformed", "garbage-segments", "wrong-audience", "wrong-issuer", "expired",
        "wrong-signature", "unknown-kid", "hs256-self-signed"])
def test_a_token_that_does_not_verify_is_rejected_and_never_stored(env, make):
    client, recorded = env
    r = _exchange(client, make())
    assert r.status_code == 401, r.text
    assert "set-cookie" not in r.headers
    assert recorded == []


def test_exchange_still_requires_a_bearer(env):
    client, _ = env
    r = client.post("/api/v1/auth/exchange", headers=_headers())
    assert r.status_code == 401 and r.json()["code"] == "MISSING_TOKEN"


def test_a_valid_token_is_still_useless_without_the_gateway_identity(env):
    """CurrentUser stays in front: a verified bearer alone does not mint a session."""
    client, _ = env
    r = client.post("/api/v1/auth/exchange", headers={"Authorization": f"Bearer {make_token()}"})
    assert r.status_code == 401
    assert "set-cookie" not in r.headers


# W-020 / X-003: the gateway's identity is the token's immutable subject (x-user-id = `sub`); the username (Envoy maps it from
# `preferred_username`) is an attribute that must agree when both sides have one.
def test_token_with_the_gateway_subject_and_the_same_username_is_accepted(env):
    client, _ = env
    r = _exchange(client, make_token(extra={"preferred_username": USER}))
    assert r.status_code == 204, r.text


def test_token_whose_preferred_username_differs_from_the_gateway_identity_is_rejected(env):
    client, _ = env
    r = _exchange(client, make_token(extra={"preferred_username": "mallory"}))
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "TOKEN_IDENTITY_MISMATCH"


def test_a_different_person_with_the_same_username_is_not_the_gateway_identity(env):
    """A username can be reused by someone else; the subject cannot. Same name, other sub -> rejected."""
    client, _ = env
    r = _exchange(client, make_token(sub=str(uuid.uuid4()), extra={"preferred_username": USER}))
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "TOKEN_IDENTITY_MISMATCH"


def test_a_renamed_user_keeps_their_identity_as_long_as_the_subject_matches(env):
    """The gateway and the token both carry the NEW username; the subject is what identifies the person."""
    client, _ = env
    r = client.post("/api/v1/auth/exchange",
                    headers={**_headers(user_name="new-name", user_id=SUBJECT),
                             "Authorization": "Bearer " + make_token(extra={"preferred_username": "new-name"})})
    assert r.status_code == 204, r.text


def test_a_gateway_without_a_subject_is_compared_by_username_as_before_dev_only(env):
    client, _ = env
    r = client.post("/api/v1/auth/exchange",
                    headers={**_headers(user_id=None), "Authorization": "Bearer " + make_token(sub="9f1c", extra={"preferred_username": USER})})
    assert r.status_code == 204, r.text
