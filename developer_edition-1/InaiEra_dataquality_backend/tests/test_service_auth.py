"""DQ-AUTH-012: outbound scoped JWTs per integration; inbound org-bound auth."""
import time

import pytest
from jose import jwt

from app.core.config import get_settings
from app.core.service_auth import (
    SCOPE_DBT_SCHEMA_TESTS_WRITE, SCOPE_FLINK_VALIDATE, SCOPE_UCS_CHECKS_WRITE,
    ServiceAuthNotConfigured, mint_service_token,
)

S = get_settings()


def _decode(token, secret, aud):
    return jwt.decode(token, secret, algorithms=["HS256"], audience=aud)


@pytest.mark.parametrize("integration,secret_attr,aud,scope", [
    ("dbtrunner", "dbtrunner_service_jwt_secret", "dbtrunner", SCOPE_DBT_SCHEMA_TESTS_WRITE),
    ("flinksql", "flinksql_service_jwt_secret", "flinksql-runner", SCOPE_FLINK_VALIDATE),
    ("ucs", "ucs_service_jwt_secret", "ucs", SCOPE_UCS_CHECKS_WRITE),
])
def test_token_claims_match_the_target_contract(integration, secret_attr, aud, scope):
    tok = mint_service_token(integration, scope=scope, org_id="org-1")
    c = _decode(tok, getattr(S, secret_attr), aud)
    assert c["iss"] == "dataquality" and c["sub"] == "dataquality"
    assert c["aud"] == aud and c["scope"] == scope and c["org_id"] == "org-1"
    assert c["jti"] and 0 < c["exp"] - c["iat"] <= 300
    assert c["iat"] <= int(time.time()) + 1


def test_dbt_token_is_project_bound_and_single_use_material():
    a = mint_service_token("dbtrunner", scope=SCOPE_DBT_SCHEMA_TESTS_WRITE, org_id="o",
                           extra_claims={"project_id": "p1"})
    b = mint_service_token("dbtrunner", scope=SCOPE_DBT_SCHEMA_TESTS_WRITE, org_id="o",
                           extra_claims={"project_id": "p1"})
    ca, cb = (_decode(t, S.dbtrunner_service_jwt_secret, "dbtrunner") for t in (a, b))
    assert ca["project_id"] == "p1" and ca["jti"] != cb["jti"]


def test_each_integration_uses_its_own_secret():
    tok = mint_service_token("dbtrunner", scope=SCOPE_DBT_SCHEMA_TESTS_WRITE, org_id="o")
    from jose.exceptions import JWTError
    with pytest.raises(JWTError):
        _decode(tok, S.flinksql_service_jwt_secret, "dbtrunner")
    with pytest.raises(JWTError):
        _decode(tok, S.dbtrunner_service_jwt_secret, "flinksql-runner")


def test_never_minted_without_org_or_with_multiple_scopes_or_reserved_override():
    with pytest.raises(ServiceAuthNotConfigured):
        mint_service_token("ucs", scope=SCOPE_UCS_CHECKS_WRITE, org_id="")
    with pytest.raises(ValueError):
        mint_service_token("ucs", scope="a b", org_id="o")
    with pytest.raises(ValueError):
        mint_service_token("ucs", scope="a", org_id="o", extra_claims={"aud": "x"})


def test_disabled_or_unconfigured_integration_refuses(monkeypatch):
    monkeypatch.setattr(S, "flinksql_enabled", False)
    with pytest.raises(ServiceAuthNotConfigured):
        mint_service_token("flinksql", scope=SCOPE_FLINK_VALIDATE, org_id="o")
    monkeypatch.setattr(S, "flinksql_enabled", True)
    monkeypatch.setattr(S, "flinksql_service_jwt_secret", None)
    with pytest.raises(ServiceAuthNotConfigured):
        mint_service_token("flinksql", scope=SCOPE_FLINK_VALIDATE, org_id="o")


def test_no_static_token_or_secret_is_ever_sent_by_clients():
    """Source guard: no client module puts a configured secret on the wire."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent / "app"
    for f in list((root / "integrations").glob("*.py")) + list((root / "services").glob("*.py")):
        text = f.read_text()
        assert "service_jwt_secret}" not in text and "internal_token" not in text, f.name
        assert "X-User-Name" not in text and "gateway_secret" not in text, f.name


# ── inbound user JWT: org-bound + permissions ──────────────────────────

@pytest.fixture
def rsa_jwks(monkeypatch):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from jose import jwk
    from app.core import auth
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()
    pub = key.public_key().public_bytes(serialization.Encoding.PEM,
                                        serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    j = jwk.construct(pub, "RS256").to_dict()
    j.update(kid="k1", alg="RS256")
    monkeypatch.setattr(auth.jwks_cache, "_keys", {"k1": j})
    monkeypatch.setattr(auth.jwks_cache, "_fetched_at", time.time())

    def make(**claims):
        now = int(time.time())
        base = {"sub": "u1", "org_id": "test-org", "iat": now, "exp": now + 300,
                "aud": S.control_plane_audience, "iss": S.control_plane_issuer,
                "roles": ["developer"]}
        base.update(claims)
        return jwt.encode(base, priv, algorithm="RS256", headers={"kid": "k1"})
    return make


async def test_inbound_token_from_another_org_is_refused(rsa_jwks):
    from fastapi import HTTPException
    from app.core.auth import validate_jwt
    with pytest.raises(HTTPException) as e:
        await validate_jwt(rsa_jwks(org_id="other-org"))
    assert e.value.status_code == 403 and e.value.detail["code"] == "ORG_MISMATCH"


async def test_inbound_token_without_org_claim_is_refused(rsa_jwks):
    from fastapi import HTTPException
    from app.core.auth import validate_jwt
    with pytest.raises(HTTPException) as e:
        await validate_jwt(rsa_jwks(org_id=None))
    assert e.value.status_code in (401, 403)


async def test_inbound_permissions_resolved_from_roles_and_scope(rsa_jwks):
    from app.core.auth import validate_jwt
    p = await validate_jwt(rsa_jwks(roles=["analyst"], scope="dq.bind openid"))
    assert p.permissions == frozenset({"dq.read", "dq.bind"})
    dev = await validate_jwt(rsa_jwks())
    assert "dq.custom_sql.write" not in dev.permissions and "dq.apply" in dev.permissions


async def test_analyst_cannot_write(client, as_principal):
    from tests.conftest import make_principal
    as_principal(make_principal(roles=["analyst"]))
    async with client as c:
        r = await c.post("/api/v1/rules", json={"name": "x", "rule_type": "not_null",
                                                "dimension": "nulls"})
    assert r.status_code == 403
    assert r.json()["detail"]["details"]["required_permission"] == "dq.rules.write"


# ── inbound service JWT (validate route) ───────────────────────────────

def _svc_token(**over):
    now = int(time.time())
    c = {"iss": "workspace", "sub": "workspace", "aud": "dataquality", "scope": "dq.rules.validate",
         "org_id": "test-org", "iat": now, "exp": now + 60, "jti": over.pop("jti", str(now) + "x")}
    c.update(over)
    return jwt.encode({k: v for k, v in c.items() if v is not None},
                      S.inbound_service_jwt_secret, algorithm="HS256")


VALIDATE = {"rule_type": "not_null", "dimension": "nulls", "column_name": "id"}


@pytest.mark.parametrize("over,code", [
    ({"aud": "dbtrunner"}, 401), ({"org_id": "other"}, 403), ({"scope": "dq.other"}, 403),
    ({"iss": "evil"}, 401), ({"org_id": None}, 403),
])
async def test_validate_route_rejects_bad_service_tokens(client, over, code):
    async with client as c:
        r = await c.post("/api/v1/render/validate/rule", json=VALIDATE,
                         headers={"X-Internal-Service-Token": _svc_token(**over)})
    assert r.status_code == code, r.text


async def test_validate_route_rejects_raw_static_token(client, monkeypatch):
    monkeypatch.setattr(S, "internal_service_token", "static-token")
    async with client as c:
        r = await c.post("/api/v1/render/validate/rule", json=VALIDATE,
                         headers={"X-Internal-Service-Token": "static-token"})
    assert r.status_code == 401


async def test_validate_route_accepts_scoped_token_once(client, db):
    tok = _svc_token(jti="unique-jti-1")
    async with client as c:
        r1 = await c.post("/api/v1/render/validate/rule", json=VALIDATE,
                          headers={"X-Internal-Service-Token": tok})
        r2 = await c.post("/api/v1/render/validate/rule", json=VALIDATE,
                          headers={"X-Internal-Service-Token": tok})
    assert r1.status_code == 200 and r1.json()["valid"] is True
    assert r2.status_code == 401 and r2.json()["detail"]["code"] == "TOKEN_REPLAYED"
