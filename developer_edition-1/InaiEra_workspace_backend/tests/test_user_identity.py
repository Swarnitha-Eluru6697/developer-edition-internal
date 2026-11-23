"""W-020 / X-003: a user is the control plane's immutable subject, everywhere; the username is only an attribute.

Before: the trusted-gateway path made the gateway's `x-user-name` (the token's preferred_username) the user's id, while the dbt and
Flink runners (and DQ) used the token's `sub`. One person therefore appeared under different actor ids in Workspace, in the
runners' audit and in resource ownership, and renaming them changed which Workspace user row (workspaces, Git credentials,
sessions) was theirs. Now the gateway must inject `x-user-id` (= `sub`); it is `Principal.user_id`, the key of the user row,
and the `sub` of every delegated token. Rows keyed by username (created before this) are re-keyed in place on the first request.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager

import pytest
from conftest import subject_for
from jose import jwt
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.auth import Principal
from app.core.config import get_settings
from app.core.service_tokens import DBTRUNNER_AUDIENCE, mint_delegated_token
from app.models import GitCredential, User, Workspace
from app.services.workspace_service import WorkspaceService

PROBE = "/api/v1/me"        # needs only the gateway principal: no DB, no other dependency


# ── the gateway contract ──────────────────────────────────────────────────
def test_the_gateway_subject_is_the_principals_identity(client, gateway_headers, monkeypatch):
    seen = {}
    from app.core import deps
    real = deps.bind_identity_contextvars
    monkeypatch.setattr(deps, "bind_identity_contextvars", lambda **kw: (seen.update(kw), real(**kw))[1])
    r = client.get(PROBE, headers=gateway_headers(user_name="alice"))
    assert r.status_code == 200
    assert seen["user_id"] == subject_for("alice") and seen["user_id"] != "alice"


def test_a_request_without_the_subject_is_refused_outside_dev(client, gateway_headers, monkeypatch):
    monkeypatch.setattr(get_settings(), "environment", "prod")
    r = client.get(PROBE, headers=gateway_headers(user_id=None))
    assert r.status_code == 401 and r.json()["detail"]["code"] == "MISSING_SUBJECT"


def test_a_request_without_the_subject_is_tolerated_only_in_dev_and_only_by_default(client, gateway_headers, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "environment", "dev")
    assert client.get(PROBE, headers=gateway_headers(user_id=None)).status_code == 200          # legacy local gateway
    monkeypatch.setattr(s, "require_user_subject", True)                                          # an operator can insist
    assert client.get(PROBE, headers=gateway_headers(user_id=None)).status_code == 401
    monkeypatch.setattr(s, "environment", "prod")
    monkeypatch.setattr(s, "require_user_subject", False)                                         # ... or explicitly relax it
    assert client.get(PROBE, headers=gateway_headers(user_id=None)).status_code == 200


@pytest.mark.parametrize("bad", ["alice", "not-a-uuid", "123", "' or 1=1 --", "00000000-0000-0000-0000-00000000000g"])
def test_a_subject_that_is_not_a_uuid_is_refused(client, gateway_headers, bad):
    r = client.get(PROBE, headers=gateway_headers(user_id=bad))
    assert r.status_code == 401 and r.json()["detail"]["code"] == "INVALID_SUBJECT"


def test_the_subject_header_is_not_trusted_without_the_gateway_secret(client, gateway_headers):
    r = client.get(PROBE, headers=gateway_headers(secret=None))
    assert r.status_code == 401 and r.json()["detail"] == "request did not come through the gateway"
    r = client.get(PROBE, headers=gateway_headers(secret="wrong"))
    assert r.status_code == 401


def test_the_subject_is_normalised_so_one_person_is_one_identity(client, gateway_headers, monkeypatch):
    seen = []
    from app.core import deps
    monkeypatch.setattr(deps, "bind_identity_contextvars", lambda **kw: seen.append(kw["user_id"]))
    sub = subject_for("alice")
    client.get(PROBE, headers=gateway_headers(user_id=sub.upper()))
    client.get(PROBE, headers=gateway_headers(user_id="  " + sub + " "))
    assert seen == [sub, sub]


# ── what the other services see ───────────────────────────────────────────
def test_delegated_tokens_carry_the_subject_never_the_username():
    p = Principal(user_id=subject_for("alice"), org_id="o", roles=["data_engineer"], email="alice", issued_at=0,
                  expires_at=0, username="alice")
    token = mint_delegated_token(secret="s" * 40, audience=DBTRUNNER_AUDIENCE, scope="dbt.projects.read", principal=p)
    claims = jwt.decode(token, "s" * 40, algorithms=["HS256"], audience=DBTRUNNER_AUDIENCE)
    assert claims["sub"] == subject_for("alice") and "alice" not in (claims["sub"],)


def test_the_catalog_is_still_asked_about_the_username(monkeypatch):
    """UCS keys its grants by name: the policy identity stays the username even though the principal's id is the subject."""
    from app.services.ucs_client import _identity_headers
    p = Principal(user_id=subject_for("alice"), org_id="o", roles=[], email="alice", issued_at=0, expires_at=0, username="alice")
    assert _identity_headers(p)["X-User-Name"] == "alice"


def test_ucs_gets_the_callers_real_groups_and_permissions_never_role_names(monkeypatch):
    """UCS (task 25) authorises on permissions and keys data grants on Keycloak groups; role names are not identity there."""
    import base64
    import json

    from app.core.config import get_settings
    from app.services.ucs_client import _identity_headers
    monkeypatch.setattr(get_settings(), "ucs_gateway_secret", "ucs-secret")
    p = Principal(user_id=subject_for("alice"), org_id="org-1", roles=["workspace_admin", "admin"], email="alice", issued_at=0,
                  expires_at=0, username="alice", groups=["/finance", "/analytics"],
                  permissions=frozenset({"ucs:explore:view", "workspace:sql:run"}))
    h = _identity_headers(p)
    dec = lambda v: json.loads(base64.b64decode(v))  # noqa: E731
    assert (h["X-User-Name"], h["X-User-Id"], h["X-Tenant-Id"]) == ("alice", subject_for("alice"), "org-1")
    assert dec(h["X-User-Groups"]) == ["/finance", "/analytics"]
    assert dec(h["X-User-Permissions"]) == ["ucs:explore:view", "workspace:sql:run"]
    assert h["X-Inaiera-Gateway-Secret"] == "ucs-secret"
    assert "X-User-Roles" not in h and "workspace_admin" not in json.dumps(h) + "".join(
        base64.b64decode(v).decode(errors="ignore") for k, v in h.items() if k in ("X-User-Groups", "X-User-Permissions"))


# ── the user row ───────────────────────────────────────────────────────────
DB_URL = os.environ.get("INTEGRATION_DATABASE_URL")


def _principal(subject, username):
    return Principal(user_id=subject, org_id="o", roles=["data_engineer"], email=username, issued_at=0, expires_at=0,
                     username=username)


async def _count(s: AsyncSession, **where):
    q = select(func.count()).select_from(User)
    for k, v in where.items():
        q = q.where(getattr(User, k) == v)
    return (await s.execute(q)).scalar_one()


@asynccontextmanager
async def db():
    """A clean users/workspaces/git_credentials on the migrated integration database (one event loop per test)."""
    engine = create_async_engine(DB_URL, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as s:
            for table in (Workspace, GitCredential, User):
                await s.execute(table.__table__.delete())
            await s.commit()
        yield factory
    finally:
        await engine.dispose()


async def _ensure(factory, subject, username):
    async with factory() as s:
        u = await WorkspaceService(s).ensure_user(_principal(subject, username))
        await s.commit()
        return u


@pytest.mark.integration
def test_a_new_user_is_keyed_by_subject_with_the_username_as_an_attribute():
    async def go():
        async with db() as f:
            sub = str(uuid.uuid4())
            u = await _ensure(f, sub, "alice")
            return u.external_id == sub, u.username

    assert asyncio.run(go()) == (True, "alice")


@pytest.mark.integration
def test_a_row_keyed_by_username_is_rekeyed_in_place_and_keeps_its_workspaces():
    async def go():
        async with db() as f:
            sub = str(uuid.uuid4())
            async with f() as s:                             # the pre-subject world: external_id = username
                old = User(external_id="alice", email="alice")
                s.add(old)
                await s.flush()
                s.add(Workspace(owner_id=old.id, name="mine", storage_prefix="p/mine"))
                await s.commit()
                old_id = old.id
            u = await _ensure(f, sub, "alice")
            async with f() as s:
                ws = (await s.execute(select(Workspace).where(Workspace.owner_id == u.id))).scalars().all()
                return (u.id == old_id, u.external_id == sub, u.username, await _count(s), [w.name for w in ws])

    assert asyncio.run(go()) == (True, True, "alice", 1, ["mine"])


@pytest.mark.integration
def test_adoption_is_idempotent_and_a_rename_keeps_the_same_row():
    async def go():
        async with db() as f:
            sub = str(uuid.uuid4())
            async with f() as s:
                s.add(User(external_id="alice", email="alice"))
                await s.commit()
            seen = []
            for name in ("alice", "alice", "alice.renamed", "alice.renamed.again"):
                u = await _ensure(f, sub, name)
                seen.append((u.id, u.username))
            async with f() as s:
                return seen, await _count(s)

    seen, n = asyncio.run(go())
    assert n == 1 and len({i for i, _ in seen}) == 1
    assert [name for _, name in seen] == ["alice", "alice", "alice.renamed", "alice.renamed.again"]   # attributes follow the person


@pytest.mark.integration
def test_two_parallel_first_requests_end_with_exactly_one_row():
    async def go():
        async with db() as f:
            sub = str(uuid.uuid4())
            async with f() as s:
                s.add(User(external_id="alice", email="alice"))
                await s.commit()
            out = await asyncio.gather(*[_ensure(f, sub, "alice") for _ in range(6)], return_exceptions=True)
            async with f() as s:
                return out, await _count(s)

    out, n = asyncio.run(go())
    assert [o for o in out if isinstance(o, Exception)] == [], out
    assert n == 1 and len({o.id for o in out}) == 1


@pytest.mark.integration
def test_two_parallel_first_requests_for_a_brand_new_user_end_with_exactly_one_row():
    async def go():
        async with db() as f:
            sub = str(uuid.uuid4())
            out = await asyncio.gather(*[_ensure(f, sub, "bob") for _ in range(6)], return_exceptions=True)
            async with f() as s:
                return out, await _count(s)

    out, n = asyncio.run(go())
    assert [o for o in out if isinstance(o, Exception)] == [], out
    assert n == 1


@pytest.mark.integration
def test_an_existing_subject_row_is_never_merged_with_a_same_named_legacy_row():
    """Two different people: the legacy row named 'alice' is NOT adopted by a subject that already has its own row."""
    async def go():
        async with db() as f:
            mine = str(uuid.uuid4())
            async with f() as s:
                s.add(User(external_id=mine, username="alice", email="alice"))
                s.add(User(external_id="alice", email="alice"))          # somebody else's pre-subject row
                await s.commit()
            u = await _ensure(f, mine, "alice")
            async with f() as s:
                return u.external_id == mine, await _count(s), await _count(s, external_id="alice")

    assert asyncio.run(go()) == (True, 2, 1)


@pytest.mark.integration
def test_when_the_subject_is_the_username_nothing_is_rekeyed():
    async def go():
        async with db() as f:
            sub = str(uuid.uuid4())
            u = await _ensure(f, sub, sub)                        # a dev gateway that keys by sub
            async with f() as s:
                return u.external_id == sub, await _count(s)

    assert asyncio.run(go()) == (True, 1)


class _FakeBackend:
    def __init__(self, vault):
        self.vault = vault

    async def get(self, path): return self.vault.get(path)
    async def put(self, path, value): self.vault[path] = value
    async def delete(self, path): self.vault.pop(path, None)


class _BrokenBackend:
    async def get(self, path): raise RuntimeError("vault down")
    async def put(self, path, value): raise RuntimeError("vault down")
    async def delete(self, path): raise RuntimeError("vault down")


async def _legacy_user_with_git_token(f):
    async with f() as s:
        old = User(external_id="alice", email="alice")
        s.add(old)
        await s.flush()
        s.add(GitCredential(user_id=old.id, provider="github", provider_user_id="alice", instance_url="",
                            vault_path="inaiera/git/alice/github"))
        await s.commit()


@pytest.mark.integration
def test_git_tokens_follow_the_user_to_the_subjects_secret_path(monkeypatch):
    vault = {"inaiera/git/alice/github": {"token": "ghp_x"}}
    from app import storage
    monkeypatch.setattr(storage, "get_secrets_backend", lambda: _FakeBackend(vault))

    async def go():
        async with db() as f:
            sub = str(uuid.uuid4())
            await _legacy_user_with_git_token(f)
            await _ensure(f, sub, "alice")
            async with f() as s:
                return sub, (await s.execute(select(GitCredential.vault_path))).scalar_one()

    sub, path = asyncio.run(go())
    assert path == f"inaiera/git/{sub}/github"
    assert vault == {f"inaiera/git/{sub}/github": {"token": "ghp_x"}}


@pytest.mark.integration
def test_a_failing_secrets_backend_never_blocks_the_login(monkeypatch):
    from app import storage
    monkeypatch.setattr(storage, "get_secrets_backend", lambda: _BrokenBackend())

    async def go():
        async with db() as f:
            sub = str(uuid.uuid4())
            await _legacy_user_with_git_token(f)
            u = await _ensure(f, sub, "alice")
            async with f() as s:
                return u.external_id == sub, (await s.execute(select(GitCredential.vault_path))).scalar_one()

    assert asyncio.run(go()) == (True, "inaiera/git/alice/github")     # still re-keyed; the old secret stays where it is
