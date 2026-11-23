"""W-011: a connection's use is authorised by a STABLE canonical resource, and the owner bypass is an explicit policy.

Before: the resource UCS was asked about was derived on every call from the connection's editable `catalog_name`, else its mutable
`name`, so a rename (or a name reused later) moved the policy identity of the same physical connection; and the creator always skipped
UCS, an accident of the code path rather than a stated policy. Now the identity is persisted on the row (`ucs_resource_fqn`: the UCS
catalog it was linked to, or `<catalog>.connection.<id>`) and the bypass is `Settings.connection_owner_bypass` (default: allowed in dev
and in the development-ungoverned profile, not in production-governed), logged whenever it is used.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.core.auth import Principal
from app.core.config import get_settings
from app.services import ucs_client
from app.services.ucs_client import (
    UcsAuthorizationError,
    authorize_connection_use,
    connection_resource_fqn,
)


def _principal(org="org-a"):
    return Principal(user_id="u", org_id=org, roles=[], email="u@x", issued_at=0, expires_at=0)


def _conn(owner, org="org-a", catalog_name=None, fqn="UNSET", name="Workspace DB"):
    cid = uuid4()
    return SimpleNamespace(id=cid, created_by_user_id=owner, org_id=org, name=name, catalog_name=catalog_name,
                           ucs_resource_fqn=f"inaiera_workspace.connection.{cid}" if fqn == "UNSET" else fqn)


@pytest.fixture
def ucs_calls(monkeypatch):
    calls = []

    async def fake_authorize(principal, *, action, object_name):
        calls.append((action, object_name))
        raise UcsAuthorizationError("denied")

    monkeypatch.setattr(ucs_client, "authorize", fake_authorize)
    return calls


@pytest.fixture
def profile(monkeypatch):
    def set_(environment="dev", profile="production-governed", bypass=None):
        s = get_settings()
        monkeypatch.setattr(s, "environment", environment)
        monkeypatch.setattr(s, "governance_profile", profile)
        monkeypatch.setattr(s, "connection_owner_bypass", bypass)
    return set_


def _use(conn, who=None, org="org-a"):
    return asyncio.run(authorize_connection_use(_principal(org), who or uuid4(), conn))


# ── the identity is persisted, never derived from names ──────────────────────
def test_the_persisted_identity_is_what_ucs_is_asked_about(ucs_calls, profile):
    profile("prod")
    conn = _conn(uuid4(), fqn="pg_sales")
    with pytest.raises(UcsAuthorizationError):
        _use(conn)
    assert ucs_calls == [("query", "pg_sales")]


def test_renaming_a_connection_never_changes_its_policy_identity(ucs_calls, profile):
    profile("prod")
    conn = _conn(uuid4())
    before = connection_resource_fqn(conn)
    conn.name = "Totally different name"
    conn.catalog_name = "someone_elses_catalog"                      # the editable fields do not move it either
    assert connection_resource_fqn(conn) == before
    with pytest.raises(UcsAuthorizationError):
        _use(conn)
    assert ucs_calls == [("query", before)]


def test_two_connections_with_the_same_name_have_different_identities():
    a, b = _conn(uuid4(), name="Warehouse"), _conn(uuid4(), name="Warehouse")
    assert connection_resource_fqn(a) != connection_resource_fqn(b)


def test_a_row_without_a_persisted_identity_gets_its_immutable_id_form_never_its_name():
    conn = _conn(uuid4(), fqn=None, name="Name that must not matter")
    assert connection_resource_fqn(conn) == f"inaiera_workspace.connection.{conn.id}"
    assert "Name that must not matter" not in connection_resource_fqn(conn)


def test_the_name_is_never_the_authorized_object(ucs_calls, profile):
    profile("prod")
    conn = _conn(uuid4(), name="pg_sales")                                # a name that looks like someone else's catalog
    with pytest.raises(UcsAuthorizationError):
        _use(conn)
    assert ucs_calls[0][1] != "pg_sales"


# ── the owner bypass is an explicit policy ───────────────────────────────────
def test_in_dev_the_owner_may_use_their_own_connection_and_it_is_logged(ucs_calls, profile, monkeypatch):
    profile("dev")
    logged = []
    monkeypatch.setattr(ucs_client.log, "info", lambda event, **kw: logged.append(event))
    me = uuid4()
    _use(_conn(me), me)
    assert ucs_calls == [] and logged == ["connection_use_owner_bypass"]


def test_in_the_production_governed_profile_the_owner_goes_through_ucs_too(ucs_calls, profile):
    profile("prod", "production-governed")
    me = uuid4()
    with pytest.raises(UcsAuthorizationError):
        _use(_conn(me), me)
    assert len(ucs_calls) == 1


def test_the_ungoverned_profile_allows_the_bypass_so_a_deployment_without_ucs_can_query_its_own_connections(ucs_calls, profile):
    profile("prod", "development-ungoverned")
    me = uuid4()
    _use(_conn(me), me)
    assert ucs_calls == []


@pytest.mark.parametrize("env,prof,setting,allowed", [
    ("prod", "production-governed", True, True),          # an operator may choose the bypass explicitly ...
    ("dev", "production-governed", False, False),         # ... or switch it off even in dev
    ("prod", "development-ungoverned", False, False),
])
def test_the_setting_overrides_the_profile_default(ucs_calls, profile, env, prof, setting, allowed):
    profile(env, prof, setting)
    me = uuid4()
    if allowed:
        _use(_conn(me), me)
        assert ucs_calls == []
    else:
        with pytest.raises(UcsAuthorizationError):
            _use(_conn(me), me)


# ── what never bypasses, whatever the policy ─────────────────────────────────
def test_other_users_connection_requires_ucs(ucs_calls, profile):
    profile("dev")
    with pytest.raises(UcsAuthorizationError):
        _use(_conn(uuid4()))
    assert len(ucs_calls) == 1


def test_owner_in_other_org_requires_ucs(ucs_calls, profile):
    profile("dev")
    me = uuid4()
    with pytest.raises(UcsAuthorizationError):
        _use(_conn(me, org="org-a"), me, org="org-b")


def test_ownerless_connection_requires_ucs(ucs_calls, profile):
    profile("dev")
    with pytest.raises(UcsAuthorizationError):
        _use(_conn(None))
