"""The invoking-user half of an entitlement check must supply real groups.

The reported symptom was that no agent could be published at all:

    Publish blocked — 1 object(s) this agent reads are not granted to it:
      • app_datasets.docs.semantic_docs — no read grant for the agent's
        service principal and the publishing user (user:demo)

The grant was not missing. Measured against the running UCS, its
/api/v1/authz/check does NOT resolve a principal's group membership
itself — it decides against the `groups` list the CALLER supplies:

    principal=padmin  groups=null               -> deny  "no matching grant"
    principal=padmin  groups=[acme-docs-readers] -> allow "granted via
                                     acme-docs-readers::kb-..-select"

Identical principal, identical object, opposite answers. With
groups=null every principal is denied, including the real deployment
admin, so check_entitlement's user half — which passed None — was not
asking a more permissive question, it was asking an unanswerable one.
The single possible reply was a denial, whatever the catalog said.

That made the two halves of the dual check behave differently for no
stated reason: the agent half had always supplied groups (from the
local Role table) and could therefore pass.

Supplying groups is a relay, not a bypass, and that distinction is what
these tests pin down: the list must come from UCS's own membership, it
must be the membership of the principal being checked, and when it
cannot be read the check must still deny.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app import ucs_client, ucs_sync


GROUP_LISTING = [
    {"group_name": "acme-docs-readers", "source": "custom", "member_names": ["alice"]},
    {"group_name": "data-engineers", "source": "oidc",
     "member_names": ["gadmin", "padmin", "alice"]},
    {"group_name": "data-analysts", "source": "custom", "member_names": []},
]


@pytest.fixture
def listing(monkeypatch):
    """UCS's own group listing, with the network call replaced.

    The transport is stubbed; the membership shape is exactly what the
    real /api/v1/admin/groups returned on the deployment this was
    diagnosed against.
    """
    monkeypatch.setattr(ucs_sync, "_groups_listing", None)
    monkeypatch.setattr(ucs_sync, "_admin_get",
                        lambda path, timeout=10.0: {"groups": GROUP_LISTING})
    return GROUP_LISTING


def test_principal_groups_reads_ucs_membership(listing):
    assert ucs_sync.principal_groups("alice") == [
        "acme-docs-readers", "data-engineers"]
    assert ucs_sync.principal_groups("gadmin") == ["data-engineers"]


def test_principal_groups_strips_the_user_prefix(listing):
    """Publish identities are spelled "user:alice"; UCS stores
    "alice". Without the strip, the lookup misses and the user is
    reported as belonging to nothing — the same denial, one layer away
    from its cause."""
    assert ucs_sync.principal_groups("user:alice") == \
        ucs_sync.principal_groups("alice")
    assert ucs_sync.principal_groups("user:alice") != []


def test_a_principal_in_no_group_really_has_none(listing):
    """The placeholder identity this bug hid behind. It must resolve to
    [] rather than to something forgiving — an unentitled identity has
    to stay unentitled once the real groups are being sent."""
    assert ucs_sync.principal_groups("user:demo") == []
    assert ucs_sync.principal_groups("nobody") == []


def _capture_authz(monkeypatch, decision="allow"):
    calls = []

    def fake(principal, groups, action, obj):
        calls.append({"principal": principal, "groups": groups,
                      "action": action, "object": obj})
        return {"decision": decision, "reason": "stub"}

    monkeypatch.setattr(ucs_client, "_authz_check", fake)
    return calls


class _Session:
    """Just enough Session for check_entitlement's Role query."""

    def __init__(self, groups):
        self._groups = groups

    def exec(self, _stmt):
        class _R:
            def __init__(self, g):
                self.group = g

        rows = [_R(g) for g in self._groups]
        return type("_Res", (), {"all": lambda self: rows})()


def test_user_half_sends_the_users_real_groups(monkeypatch, listing):
    """The regression itself. Before the fix the user call carried
    groups=None, which UCS answers "no matching grant" for everyone."""
    calls = _capture_authz(monkeypatch)

    ucs_client.check_entitlement(
        _Session(["acme-docs-readers"]), "a1", "Revenue Analysis Agent",
        "user:alice", "app_datasets.docs.semantic_docs")

    user_call = next(c for c in calls if c["principal"] == "user:alice")
    assert user_call["groups"] is not None, \
        "the user half went out with groups=None — UCS denies that unconditionally"
    assert "acme-docs-readers" in user_call["groups"]


def test_agent_half_still_sends_only_its_own_bindings(monkeypatch, listing):
    """The two halves must stay independent. The agent principal is
    entitled through the groups bound to IT, and must not inherit the
    publishing user's membership — that would let any agent read
    whatever its publisher can."""
    calls = _capture_authz(monkeypatch)

    ucs_client.check_entitlement(
        _Session(["data-analysts"]), "a1", "Revenue Analysis Agent",
        "user:alice", "app_datasets.docs.semantic_docs")

    agent_call = next(c for c in calls if c["principal"].startswith("agent:"))
    assert agent_call["groups"] == ["data-analysts"]
    assert "acme-docs-readers" not in (agent_call["groups"] or [])


def test_unreadable_membership_denies_rather_than_passes(monkeypatch):
    """Fail closed. If UCS membership cannot be read, the check must not
    fall back to anything permissive — and the object must still be
    reported as not granted."""
    monkeypatch.setattr(ucs_sync, "_groups_listing", None)

    def boom(path, timeout=10.0):
        raise ucs_client.UCSUnavailable("UCS unreachable")

    monkeypatch.setattr(ucs_sync, "_admin_get", boom)
    calls = _capture_authz(monkeypatch, decision="deny")

    result = ucs_client.check_entitlement(
        _Session([]), "a1", "Revenue Analysis Agent",
        "user:alice", "app_datasets.docs.semantic_docs")

    user_call = next(c for c in calls if c["principal"] == "user:alice")
    assert user_call["groups"] is None
    assert result["invoking_user_ok"] is False
    assert result["resolved"] is False
