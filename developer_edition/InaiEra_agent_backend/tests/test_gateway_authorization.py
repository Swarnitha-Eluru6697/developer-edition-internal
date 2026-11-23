"""
Gateway authorization: the north-south trust boundary
(InaiEra_dataplane_GatewayAuth) as adopted by this service.

Five fixed headers, two fail-closed checks. What these tests exist to
hold in place is the fail-closed half — the part that is easy to break
without noticing, because breaking it makes MORE requests succeed:

  - TRUST_ENVOY_HEADERS must be the literal lowercase "true". "True",
    "1" and "yes" are all false here, so a deployment that sets one of
    those gets no trust rather than silent trust.
  - With no ENVOY_INTERNAL_SECRET configured, nothing can prove it came
    through the gateway, so no identity is accepted at all.
  - A mismatched secret is refused.
  - A request that fails either check has no principal and therefore no
    permissions; role headers are never consulted.

The env vars are read per call rather than at import precisely so this
is testable, and so a deployment can flip trust without a rebuild.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app import gateway_identity as gi

SECRET = "test-gateway-secret-not-a-real-one"


class FakeRequest:
    """Only `.headers` is touched by the module under test."""

    def __init__(self, **headers):
        self.headers = {k.lower().replace("_", "-"): v for k, v in headers.items()}


def _gateway_headers(secret=SECRET, name="alice", roles="some-realm-role",
                     groups="data-engineers", tenant="prod_metalake",
                     permissions="agent:studio:edit"):
    return FakeRequest(**{
        "x-inaiera-gateway-secret": secret,
        "x-user-name": name,
        "x-user-roles": roles,
        "x-user-groups": groups,
        "x-tenant-id": tenant,
        "x-user-permissions": permissions,
    })


@pytest.fixture
def trusted(monkeypatch):
    monkeypatch.setenv("TRUST_ENVOY_HEADERS", "true")
    monkeypatch.setenv("ENVOY_INTERNAL_SECRET", SECRET)


# --- no role vocabulary at all -------------------------------------------

def test_this_service_declares_no_role_vocabulary():
    assert not hasattr(gi, "ROLES")
    assert not hasattr(gi, "role_from_gateway_headers")


def test_no_identity_when_trust_is_unset(monkeypatch):
    monkeypatch.delenv("TRUST_ENVOY_HEADERS", raising=False)
    monkeypatch.setenv("ENVOY_INTERNAL_SECRET", SECRET)
    assert gi.identity_from_gateway_headers(_gateway_headers()) is None


@pytest.mark.parametrize("value", ["True", "TRUE", "1", "yes", "on", "", " ", "true "])
def test_only_the_literal_lowercase_true_enables_trust(monkeypatch, value):
    """The checklist is explicit that this is case-sensitive. A
    deployment that writes True/1/yes must get NO trust, which is the
    safe direction — this test is what stops someone "helpfully"
    widening the parse."""
    monkeypatch.setenv("TRUST_ENVOY_HEADERS", value)
    monkeypatch.setenv("ENVOY_INTERNAL_SECRET", SECRET)
    assert gi.gateway_trust_enabled() is False
    assert gi.identity_from_gateway_headers(_gateway_headers()) is None


def test_the_local_check_matches_the_librarys_own_comparison(monkeypatch):
    """`make_gateway_dependency` decides trust with a bare
    `os.environ.get(...) == "true"`. This service's own helper must agree
    exactly, or the two paths disagree about whether a caller is
    authenticated — which is what happened when the helper lowercased
    and stripped first."""
    for value, expected in [("true", True), (" true", False), ("true ", False),
                            ("True", False)]:
        monkeypatch.setenv("TRUST_ENVOY_HEADERS", value)
        library_view = os.environ.get("TRUST_ENVOY_HEADERS") == "true"
        assert gi.gateway_trust_enabled() is expected
        assert gi.gateway_trust_enabled() is library_view


# --- fail closed: the shared secret ---------------------------------------

def test_no_identity_when_no_secret_is_configured(monkeypatch):
    """Nothing can prove gateway passage, so nothing is trusted — even
    though the request presents a complete, well-formed header set."""
    monkeypatch.setenv("TRUST_ENVOY_HEADERS", "true")
    monkeypatch.delenv("ENVOY_INTERNAL_SECRET", raising=False)
    assert gi.identity_from_gateway_headers(_gateway_headers()) is None


def test_no_identity_on_a_mismatched_secret(trusted):
    assert gi.identity_from_gateway_headers(
        _gateway_headers(secret="wrong-secret")) is None


def test_no_identity_when_the_secret_header_is_absent(trusted):
    r = _gateway_headers()
    del r.headers["x-inaiera-gateway-secret"]
    assert gi.identity_from_gateway_headers(r) is None


# --- the happy path --------------------------------------------------------

def test_identity_is_taken_from_x_user_name_once_both_checks_pass(trusted):
    assert gi.identity_from_gateway_headers(_gateway_headers(name="bob")) == "bob"


def test_permissions_come_from_x_user_permissions(trusted):
    p = gi.principal_from_request(_gateway_headers(permissions="agent:studio:edit,agent:runs:view"))
    assert p.has("agent:studio:edit") and p.has("agent:runs:view")
    assert not p.has("agent:settings:edit")


def test_role_headers_grant_nothing(trusted):
    """x-user-roles is display/audit only: an admin-looking role with no
    permissions is a principal with no permissions."""
    p = gi.principal_from_request(_gateway_headers(roles="admin,superuser", permissions=""))
    assert p is not None and not p.permissions


def test_groups_are_parsed_by_the_library(trusted):
    r = _gateway_headers(groups="data-engineers, analysts")
    assert gi.groups_from_gateway_headers(r) == ["data-engineers", "analysts"]


# --- the part that matters most -------------------------------------------

def test_a_failed_check_has_no_principal_and_no_permissions(monkeypatch):
    """A request that did not come through the gateway must not merely
    lack an identity — it must not carry any permission it could act on."""
    monkeypatch.setenv("TRUST_ENVOY_HEADERS", "true")
    monkeypatch.setenv("ENVOY_INTERNAL_SECRET", SECRET)
    forged = _gateway_headers(secret="not-the-secret")
    assert gi.identity_from_gateway_headers(forged) is None
    assert gi.principal_from_request(forged) is None


def test_trust_off_ignores_even_a_correct_secret(monkeypatch):
    monkeypatch.setenv("TRUST_ENVOY_HEADERS", "false")
    monkeypatch.setenv("ENVOY_INTERNAL_SECRET", SECRET)
    r = _gateway_headers()
    assert gi.identity_from_gateway_headers(r) is None
    assert gi.principal_from_request(r) is None


# --- the refusal message ---------------------------------------------------
#
# The 401 body is the only thing an operator debugging this actually
# sees. It used to name X-Auth-Request-Email — the LEGACY header, not one
# of the gateway contract's five — so it pointed away from the real cause
# for anyone testing the gateway.

def _detail(monkeypatch, request, **env):
    for k, v in env.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    from app.auth import _identity_refusal_detail
    return _identity_refusal_detail(request)


def test_refusal_names_trust_being_off(monkeypatch):
    d = _detail(monkeypatch, _gateway_headers(),
                TRUST_ENVOY_HEADERS="false", ENVOY_INTERNAL_SECRET=SECRET)
    assert "TRUST_ENVOY_HEADERS" in d
    assert "disabled" in d


def test_refusal_names_a_missing_server_side_secret(monkeypatch):
    d = _detail(monkeypatch, _gateway_headers(),
                TRUST_ENVOY_HEADERS="true", ENVOY_INTERNAL_SECRET=None)
    assert "no gateway secret is configured" in d


def test_refusal_distinguishes_a_mismatched_secret_from_an_absent_one(monkeypatch):
    absent = _gateway_headers()
    del absent.headers["x-inaiera-gateway-secret"]
    d_absent = _detail(monkeypatch, absent,
                       TRUST_ENVOY_HEADERS="true", ENVOY_INTERNAL_SECRET=SECRET)
    assert "was absent" in d_absent

    d_wrong = _detail(monkeypatch, _gateway_headers(secret="nope"),
                      TRUST_ENVOY_HEADERS="true", ENVOY_INTERNAL_SECRET=SECRET)
    assert "did not match" in d_wrong
    assert d_absent != d_wrong


def test_refusal_names_a_missing_username_once_the_secret_matched(monkeypatch):
    r = _gateway_headers()
    del r.headers["x-user-name"]
    d = _detail(monkeypatch, r, TRUST_ENVOY_HEADERS="true",
                ENVOY_INTERNAL_SECRET=SECRET)
    assert "x-user-name was absent" in d


def test_refusal_names_the_gateway_header(monkeypatch):
    d = _detail(monkeypatch, _gateway_headers(secret="nope"),
                TRUST_ENVOY_HEADERS="true", ENVOY_INTERNAL_SECRET=SECRET)
    assert "x-inaiera-gateway-secret" in d
    assert "X-Auth-Request-Email" not in d


def test_refusal_says_the_headers_must_come_from_the_gateway(monkeypatch):
    """Otherwise the obvious next move is to set them by hand."""
    d = _detail(monkeypatch, _gateway_headers(secret="nope"),
                TRUST_ENVOY_HEADERS="true", ENVOY_INTERNAL_SECRET=SECRET)
    assert "not by the client" in d


def test_refusal_never_echoes_the_secret(monkeypatch):
    """A 401 body goes back to the caller."""
    d = _detail(monkeypatch, _gateway_headers(secret="nope"),
                TRUST_ENVOY_HEADERS="true", ENVOY_INTERNAL_SECRET=SECRET)
    assert SECRET not in d
    assert "nope" not in d
