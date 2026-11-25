"""WS-009: database-connection egress is allowlist-based — private and
internal destinations are denied unless the operator configured them."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.core import egress_policy
from app.core.egress_policy import EgressPolicyError, assert_host_allowed


def _settings(monkeypatch, networks=(), hosts=(), public=True):
    monkeypatch.setattr(egress_policy, "get_settings", lambda: SimpleNamespace(
        connection_allowed_networks=list(networks), connection_allowed_hosts=list(hosts),
        connection_allow_public=public,
    ))


def _check(host):
    asyncio.run(assert_host_allowed(host))


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "169.254.169.254", "0.0.0.0", "224.0.0.1", "fe80::1"])
def test_always_blocked_even_if_listed(monkeypatch, host):
    _settings(monkeypatch, networks=["0.0.0.0/0", "::/0"], hosts=[host])
    with pytest.raises(EgressPolicyError):
        _check(host)


@pytest.mark.parametrize("host", ["10.1.2.3", "172.20.0.5", "192.168.1.10", "100.64.0.1", "fd00::1"])
def test_private_denied_by_default(monkeypatch, host):
    _settings(monkeypatch)
    with pytest.raises(EgressPolicyError, match="private address"):
        _check(host)


def test_private_allowed_when_network_configured(monkeypatch):
    _settings(monkeypatch, networks=["10.20.0.0/16"])
    _check("10.20.5.6")
    with pytest.raises(EgressPolicyError):
        _check("10.21.0.1")            # a different private range


def test_private_allowed_for_listed_host(monkeypatch):
    _settings(monkeypatch, hosts=[".db.acme.internal"])

    async def fake_getaddrinfo(self, host, port):
        return [(None, None, None, None, ("10.9.9.9", 0))]
    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", fake_getaddrinfo)
    _check("pg.db.acme.internal")
    with pytest.raises(EgressPolicyError):
        _check("pg.other.internal")     # same address, unlisted name


def test_public_allowed_unless_disabled(monkeypatch):
    _settings(monkeypatch)
    _check("8.8.8.8")
    _settings(monkeypatch, public=False)
    with pytest.raises(EgressPolicyError, match="public address"):
        _check("8.8.8.8")
    _settings(monkeypatch, public=False, networks=["8.8.8.0/24"])
    _check("8.8.8.8")


def test_ipv4_mapped_ipv6_cannot_bypass(monkeypatch):
    _settings(monkeypatch)
    with pytest.raises(EgressPolicyError):
        _check("::ffff:169.254.169.254")
    with pytest.raises(EgressPolicyError):
        _check("::ffff:10.0.0.1")


def test_empty_host_refused(monkeypatch):
    _settings(monkeypatch)
    with pytest.raises(EgressPolicyError):
        _check("")
