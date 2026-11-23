"""RUN-FLINK-009 — validate_jwt() checked signature/issuer/audience but
never compared the token's org_id claim to this data plane's own
settings.org_id, unlike every other service (see e.g. dbt-runner's
app.core.auth.validate_jwt, which this mirrors). A validly-signed token
for a DIFFERENT organization — same control plane, same issuer, same
audience — would have been accepted here, since nothing checked which
org it actually belonged to.

Exercises the real validate_jwt() against a real RSA-signed JWT, with
only the outbound JWKS fetch mocked (no network dependency), rather than
mocking jose.jwt.decode itself — the whole point is proving the org
check actually runs as part of real token validation, not in isolation
from it.
"""
from __future__ import annotations

import time
import uuid

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwk, jwt

from app.core.auth import Principal, reset_jwks_cache, validate_jwt
from app.core.config import Settings

ISSUER = "http://test-issuer"
AUDIENCE = "flinksql-runner"
KID = "test-kid-1"


@pytest.fixture(scope="module")
def rsa_keys() -> tuple[str, dict]:
    """One RSA keypair for this module: (private_pem, jwks_dict)."""
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = private.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    public_jwk = jwk.construct(public_pem, "RS256").to_dict()
    public_jwk["kid"] = KID
    public_jwk["alg"] = "RS256"
    public_jwk["use"] = "sig"
    return private_pem, {"keys": [public_jwk]}


@pytest.fixture(autouse=True)
def _fresh_jwks_cache():
    reset_jwks_cache()
    yield
    reset_jwks_cache()


@pytest.fixture
def jwks_fetches():
    return []


@pytest.fixture
def mock_jwks(monkeypatch, rsa_keys, jwks_fetches):
    """Mock only the outbound JWKS fetch, not jwt.decode itself; records
    every fetch so caching can be asserted."""
    _, jwks = rsa_keys

    class _FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return jwks

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url):
            jwks_fetches.append(url)
            return _FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: _FakeClient())


def _mint(rsa_keys, **overrides) -> str:
    private_pem, _ = rsa_keys
    now = int(time.time())
    claims = {
        "iss": ISSUER, "aud": AUDIENCE, "sub": str(uuid.uuid4()),
        "org_id": "org-42", "permissions": ["workspace:catalog:view"], "email": "dev@example.com",
        "iat": now, "exp": now + 900,
    }
    claims.update(overrides)
    return jwt.encode(claims, private_pem, algorithm="RS256", headers={"kid": KID})


def _settings(**overrides) -> Settings:
    base = {
        "control_plane_jwks_url": "http://test/jwks", "control_plane_issuer": ISSUER,
        "control_plane_audience": AUDIENCE, "org_id": "org-42",
    }
    base.update(overrides)
    return Settings(**base)


async def test_a_token_for_this_data_planes_own_org_is_accepted(monkeypatch, rsa_keys, mock_jwks):
    monkeypatch.setattr("app.core.auth.get_settings", lambda: _settings(org_id="org-42"))
    principal = await validate_jwt(_mint(rsa_keys, org_id="org-42"))
    assert isinstance(principal, Principal)
    assert principal.org_id == "org-42"


async def test_a_token_for_a_different_org_is_rejected(monkeypatch, rsa_keys, mock_jwks):
    """The exact vulnerability this finding describes: a validly-signed
    token — correct issuer, correct audience, correct signature — but for
    another organization entirely. This data plane serves only org-42."""
    monkeypatch.setattr("app.core.auth.get_settings", lambda: _settings(org_id="org-42"))

    with pytest.raises(Exception) as exc_info:
        await validate_jwt(_mint(rsa_keys, org_id="some-other-org"))

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["code"] == "ORG_MISMATCH"


async def test_a_token_with_no_org_claim_at_all_is_rejected(monkeypatch, rsa_keys, mock_jwks):
    """No silent 'local-dev' fallback that would let an org-less token
    slip through unchecked — matching dbt-runner's own strict comparison."""
    monkeypatch.setattr("app.core.auth.get_settings", lambda: _settings(org_id="org-42"))

    token = _mint(rsa_keys)
    # jose's jwt.encode keeps whatever's in the claims dict; explicitly
    # drop org_id rather than relying on _mint()'s default.
    claims = jwt.get_unverified_claims(token)
    del claims["org_id"]
    private_pem, _ = rsa_keys
    token_without_org = jwt.encode(claims, private_pem, algorithm="RS256", headers={"kid": KID})

    with pytest.raises(Exception) as exc_info:
        await validate_jwt(token_without_org)
    assert exc_info.value.status_code == 403


async def test_permissions_claim_is_propagated_onto_the_principal_and_roles_are_ignored(monkeypatch, rsa_keys, mock_jwks):
    monkeypatch.setattr("app.core.auth.get_settings", lambda: _settings(org_id="org-42"))
    principal = await validate_jwt(_mint(rsa_keys, org_id="org-42", permissions=["workspace:flink:edit"],
                                         roles=["admin", "workspace_admin"]))
    assert principal.permissions == frozenset({"workspace:flink:edit"})
    assert not hasattr(principal, "roles")


async def test_missing_permissions_claim_defaults_to_none(monkeypatch, rsa_keys, mock_jwks):
    monkeypatch.setattr("app.core.auth.get_settings", lambda: _settings(org_id="org-42"))
    token = _mint(rsa_keys, org_id="org-42")
    claims = jwt.get_unverified_claims(token)
    del claims["permissions"]
    private_pem, _ = rsa_keys
    token_without_permissions = jwt.encode(claims, private_pem, algorithm="RS256", headers={"kid": KID})

    principal = await validate_jwt(token_without_permissions)
    assert principal.permissions == frozenset()


async def test_a_token_with_the_wrong_signature_is_still_rejected_before_the_org_check(
    monkeypatch, rsa_keys, mock_jwks,
):
    """Sanity check that the new org check didn't accidentally short-
    circuit ahead of, or replace, the existing signature/issuer/audience
    validation."""
    monkeypatch.setattr("app.core.auth.get_settings", lambda: _settings(org_id="org-42"))
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other_pem = other_key.private_bytes(
        encoding=serialization.Encoding.PEM, format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    now = int(time.time())
    bad_token = jwt.encode(
        {"iss": ISSUER, "aud": AUDIENCE, "sub": "x", "org_id": "org-42",
         "iat": now, "exp": now + 900},
        other_pem, algorithm="RS256", headers={"kid": KID},
    )
    with pytest.raises(Exception) as exc_info:
        await validate_jwt(bad_token)
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail["code"] == "INVALID_TOKEN"


async def test_a_token_without_sub_is_401_not_500(monkeypatch, rsa_keys, mock_jwks):
    monkeypatch.setattr("app.core.auth.get_settings", lambda: _settings(org_id="org-42"))
    token = _mint(rsa_keys, org_id="org-42")
    claims = jwt.get_unverified_claims(token)
    del claims["sub"]
    private_pem, _ = rsa_keys
    no_sub = jwt.encode(claims, private_pem, algorithm="RS256", headers={"kid": KID})
    with pytest.raises(Exception) as exc_info:
        await validate_jwt(no_sub)
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail["code"] == "INVALID_TOKEN"


async def test_jwks_is_cached_for_the_configured_ttl(monkeypatch, rsa_keys, mock_jwks, jwks_fetches):
    monkeypatch.setattr("app.core.auth.get_settings",
                        lambda: _settings(org_id="org-42", jwks_cache_ttl_seconds=3600))
    for _ in range(5):
        await validate_jwt(_mint(rsa_keys, org_id="org-42"))
    assert len(jwks_fetches) == 1


async def test_jwks_is_refetched_after_the_ttl_expires(monkeypatch, rsa_keys, mock_jwks, jwks_fetches):
    monkeypatch.setattr("app.core.auth.get_settings",
                        lambda: _settings(org_id="org-42", jwks_cache_ttl_seconds=0))
    await validate_jwt(_mint(rsa_keys, org_id="org-42"))
    await validate_jwt(_mint(rsa_keys, org_id="org-42"))
    assert len(jwks_fetches) == 2


async def test_rotated_signing_key_triggers_one_early_refresh(monkeypatch, rsa_keys, jwks_fetches):
    """A cached JWKS without the new key: one (rate-limited) refresh, then
    the token verifies — no waiting out the whole TTL."""
    import app.core.auth as auth_mod
    _, new_jwks = rsa_keys
    monkeypatch.setattr("app.core.auth.get_settings",
                        lambda: _settings(org_id="org-42", jwks_min_refresh_interval_seconds=0))
    old_jwks = {"keys": []}
    served = [old_jwks, new_jwks]

    async def _fetch(url):
        jwks_fetches.append(url)
        return served.pop(0) if served else new_jwks

    monkeypatch.setattr(auth_mod, "_fetch_jwks", _fetch)
    await auth_mod.get_jwks()                       # caches the pre-rotation document
    principal = await validate_jwt(_mint(rsa_keys, org_id="org-42"))
    assert principal.org_id == "org-42"
    assert len(jwks_fetches) == 2


# ── DBT-002: own audience only; externally issued tokens are short-lived ──────────────────────
@pytest.mark.parametrize("aud", ["workspace.inaiera", "dbtrunner", "dq", "", "  "])
def test_settings_refuse_another_services_audience(aud):
    with pytest.raises(ValueError, match="CONTROL_PLANE_AUDIENCE"):
        _settings(control_plane_audience=aud)


async def test_a_long_lived_external_token_is_refused(monkeypatch, rsa_keys, mock_jwks):
    monkeypatch.setattr("app.core.auth.get_settings", lambda: _settings())
    now = int(time.time())
    with pytest.raises(Exception) as exc_info:
        await validate_jwt(_mint(rsa_keys, org_id="org-42", iat=now, exp=now + 8 * 3600))
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail["code"] == "TOKEN_LIFETIME_TOO_LONG"


async def test_an_external_token_at_the_lifetime_cap_is_accepted(monkeypatch, rsa_keys, mock_jwks):
    monkeypatch.setattr("app.core.auth.get_settings", lambda: _settings())
    now = int(time.time())
    principal = await validate_jwt(_mint(rsa_keys, org_id="org-42", iat=now, exp=now + _settings().external_jwt_max_lifetime_seconds))
    assert principal.org_id == "org-42"
