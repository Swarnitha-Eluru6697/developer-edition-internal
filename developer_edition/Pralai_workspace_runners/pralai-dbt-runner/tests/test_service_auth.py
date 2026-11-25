"""RUN-DBT-005 / DQ-AUTH-012 — scoped service tokens on the REAL route
POST /api/v1/projects/{project_id}/schema-tests (app/api/projects.py),
exercised through the real app (app.main) against the migrated test DB.

Uses the configured org (settings.org_id) rather than relying on env
setdefault ordering, so it is independent of what conftest sets ORG_ID to.
"""
from __future__ import annotations

import time
import uuid

import httpx
import pytest
from jose import jwt
from jose.exceptions import JWTError

from app.core.config import Settings, get_settings
from app.core.service_auth import SERVICE_AUDIENCE, UCS_AUDIENCE, mint_scoped_token
from app.main import app
from app.models import Project

SERVICE_SECRET = "svc-signing-secret"
LEGACY_RAW = "legacy-raw-internal-token"
SCOPE = "dbt.schema-tests.write"


@pytest.fixture
def svc_settings(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "service_jwt_secret", SERVICE_SECRET)
    monkeypatch.setattr(s, "internal_service_token", LEGACY_RAW)
    monkeypatch.setattr(s, "service_jwt_allowed_issuers", ["dataquality"])
    monkeypatch.setattr(s, "service_jwt_allowed_subjects", ["dataquality"])
    return s


@pytest.fixture
async def project_id(run_db, svc_settings) -> uuid.UUID:
    async with run_db() as db:
        p = Project(org_id=svc_settings.org_id, name="analytics",
                    git_url="https://github.com/acme/analytics.git", git_ref="main", engine="pg_lake")
        db.add(p)
        await db.commit()
        return p.id


@pytest.fixture
async def api(run_db):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _token(project_id, *, secret=SERVICE_SECRET, drop=(), **overrides) -> str:
    now = int(time.time())
    claims = {
        "iss": "dataquality", "sub": "dataquality", "aud": SERVICE_AUDIENCE, "scope": SCOPE,
        "org_id": get_settings().org_id, "project_id": str(project_id), "jti": uuid.uuid4().hex,
        "iat": now, "exp": now + 60,
    }
    claims.update(overrides)
    for k in drop:
        claims.pop(k, None)
    return jwt.encode(claims, secret, algorithm="HS256")


async def _post(api, project_id, token):
    headers = {"X-Internal-Service-Token": token} if token is not None else {}
    return await api.post(f"/api/v1/projects/{project_id}/schema-tests", headers=headers,
                          json={"target_table": "stg_orders", "yaml_content": "models: []\n"})


async def test_valid_token_is_accepted_and_persists(api, project_id, run_db):
    r = await _post(api, project_id, _token(project_id))
    assert r.status_code == 204, r.text
    async with run_db() as db:
        assert (await db.get(Project, project_id)).dq_schema_tests == {"stg_orders": "models: []\n"}


async def test_raw_legacy_token_is_rejected(api, project_id):
    r = await _post(api, project_id, LEGACY_RAW)
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "RAW_TOKEN_REJECTED"


async def test_token_signed_with_legacy_raw_token_is_rejected(api, project_id):
    """The original weakness: the raw token was also the signing key."""
    r = await _post(api, project_id, _token(project_id, secret=LEGACY_RAW))
    assert r.status_code == 401


def test_settings_refuse_identical_signing_and_legacy_secrets():
    with pytest.raises(ValueError, match="must differ"):
        Settings(service_jwt_secret="same", internal_service_token="same")


async def test_missing_token_is_rejected(api, project_id):
    assert (await _post(api, project_id, None)).status_code == 401


async def test_non_jwt_and_wrong_key_are_rejected(api, project_id):
    assert (await _post(api, project_id, "not-a-jwt")).status_code == 401
    assert (await _post(api, project_id, _token(project_id, secret="wrong"))).status_code == 401


async def test_not_configured_rejects_everything(api, project_id, monkeypatch, svc_settings):
    monkeypatch.setattr(svc_settings, "service_jwt_secret", None)
    r = await _post(api, project_id, _token(project_id))
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "SERVICE_AUTH_NOT_CONFIGURED"


@pytest.mark.parametrize("claim", ["exp", "iat", "jti", "sub", "iss", "org_id", "project_id"])
async def test_missing_required_claim_is_rejected(api, project_id, claim):
    r = await _post(api, project_id, _token(project_id, drop=(claim,)))
    assert r.status_code == 401, (claim, r.text)


async def test_missing_org_is_rejected(api, project_id):
    r = await _post(api, project_id, _token(project_id, drop=("org_id",)))
    assert r.status_code == 401


async def test_wrong_org_is_rejected(api, project_id):
    r = await _post(api, project_id, _token(project_id, org_id="some-other-org"))
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "ORG_MISMATCH"


async def test_token_for_another_project_is_rejected(api, project_id, run_db, svc_settings):
    async with run_db() as db:
        other = Project(org_id=svc_settings.org_id, name="other",
                        git_url="https://github.com/acme/other.git", git_ref="main", engine="pg_lake")
        db.add(other)
        await db.commit()
        other_id = other.id
    r = await _post(api, other_id, _token(project_id))
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "PROJECT_MISMATCH"
    async with run_db() as db:
        assert (await db.get(Project, other_id)).dq_schema_tests is None


async def test_replayed_token_is_rejected(api, project_id):
    token = _token(project_id)
    assert (await _post(api, project_id, token)).status_code == 204
    r = await _post(api, project_id, token)
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "TOKEN_REPLAYED"


async def test_disallowed_issuer_and_subject_are_rejected(api, project_id):
    assert (await _post(api, project_id, _token(project_id, iss="workspace"))).status_code == 401
    assert (await _post(api, project_id, _token(project_id, sub="someone"))).status_code == 401


async def test_wrong_audience_is_rejected(api, project_id):
    assert (await _post(api, project_id, _token(project_id, aud="flinksql-runner"))).status_code == 401


async def test_wrong_scope_is_403(api, project_id):
    r = await _post(api, project_id, _token(project_id, scope="flink.jobs.validate"))
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "INSUFFICIENT_SCOPE"


async def test_expired_token_is_rejected(api, project_id):
    now = int(time.time())
    assert (await _post(api, project_id, _token(project_id, iat=now - 600, exp=now - 300))).status_code == 401


async def test_overlong_lifetime_is_rejected(api, project_id):
    now = int(time.time())
    assert (await _post(api, project_id, _token(project_id, iat=now, exp=now + 3600))).status_code == 401


async def test_future_iat_is_rejected(api, project_id):
    now = int(time.time())
    assert (await _post(api, project_id, _token(project_id, iat=now + 600, exp=now + 700))).status_code == 401


async def test_project_in_another_org_is_404_even_with_valid_token(api, run_db, svc_settings):
    async with run_db() as db:
        foreign = Project(org_id="org-other-tenant", name="f", git_url="https://github.com/acme/f.git",
                          git_ref="main", engine="pg_lake")
        db.add(foreign)
        await db.commit()
        fid = foreign.id
    r = await _post(api, fid, _token(fid))
    assert r.status_code == 404


# ---------------------------------------------------------------------
# Outbound: mint_scoped_token() (RUN-DBT-007)
# ---------------------------------------------------------------------
def test_mint_scoped_token_round_trip():
    token = mint_scoped_token(secret="ucs-secret", audience=UCS_AUDIENCE, scope="ucs.lineage.write",
                              subject="dbtrunner", org_id="org-42")
    claims = jwt.decode(token, "ucs-secret", algorithms=["HS256"], audience=UCS_AUDIENCE)
    assert claims["scope"] == "ucs.lineage.write"
    assert claims["sub"] == "dbtrunner" and claims["org_id"] == "org-42"
    assert claims["jti"] and 0 < claims["exp"] - claims["iat"] <= 300


def test_minted_tokens_have_unique_jtis():
    a = jwt.get_unverified_claims(mint_scoped_token(secret="s", audience="a", scope="x", subject="d"))
    b = jwt.get_unverified_claims(mint_scoped_token(secret="s", audience="a", scope="x", subject="d"))
    assert a["jti"] != b["jti"]


def test_token_minted_for_ucs_does_not_verify_as_ours():
    token = mint_scoped_token(secret="shared", audience=UCS_AUDIENCE, scope="ucs.lineage.write",
                              subject="dbtrunner")
    with pytest.raises(JWTError):
        jwt.decode(token, "shared", algorithms=["HS256"], audience=SERVICE_AUDIENCE)
