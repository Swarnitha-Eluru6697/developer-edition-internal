"""Egress policy for server-side connections to user-supplied hosts (WS-009).

`DatabaseConnection.host`/`.port` are typed in by whoever registers a
connection — nothing constrains them. Without this check, the connection
test, catalog browse and connection SQL paths are an SSRF primitive: point a
"connection" at the cloud metadata endpoint, a loopback port on the pod, or
another internal service, and the response comes back through a test result
or error message.

Rules, for every address the host resolves to (so a DNS name can't route
around them):
  - loopback, link-local (169.254.0.0/16 incl. the metadata endpoint,
    fe80::/10), multicast, reserved, unspecified: always denied;
  - any other non-public address (RFC1918, CGNAT 100.64/10, IPv6 ULA, …):
    denied unless inside `connection_allowed_networks` — a customer's
    database usually IS on a private VPC address, but which private ranges
    are databases (vs. the cluster's own services) is the operator's call,
    made explicitly in config;
  - public addresses: allowed while `connection_allow_public` is true.
A host on `connection_allowed_hosts` skips the private-range rule (still
never loopback/link-local).
"""
from __future__ import annotations

import asyncio
import ipaddress

from app.core.config import get_settings


class EgressPolicyError(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _host_listed(host: str, allowed: list[str]) -> bool:
    h = host.lower().rstrip(".")
    for entry in allowed:
        e = entry.lower()
        if (e.startswith(".") and h.endswith(e)) or h == e:
            return True
    return False


async def assert_host_allowed(host: str) -> None:
    """Raise EgressPolicyError unless `host` is a permitted destination."""
    if not host:
        raise EgressPolicyError("no host provided")
    settings = get_settings()

    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        loop = asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(host, None)
        except OSError as e:
            raise EgressPolicyError(f"could not resolve host: {e}") from e
        addresses = [ipaddress.ip_address(info[4][0]) for info in infos]

    networks = []
    for cidr in settings.connection_allowed_networks:
        try:
            networks.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError:
            continue
    host_listed = _host_listed(host, settings.connection_allowed_hosts)

    for addr in addresses:
        if getattr(addr, "ipv4_mapped", None):
            addr = addr.ipv4_mapped
        if addr.is_loopback or addr.is_link_local or addr.is_reserved \
           or addr.is_multicast or addr.is_unspecified:
            raise EgressPolicyError(
                f"host resolves to a disallowed address ({addr}) — loopback, link-local and "
                "reserved ranges are never valid targets for a database connection"
            )
        in_allowed_network = any(addr in n for n in networks)
        if addr.is_global:
            if not (settings.connection_allow_public or in_allowed_network or host_listed):
                raise EgressPolicyError(
                    f"host resolves to a public address ({addr}) and this deployment only permits "
                    "connections to its configured networks"
                )
            continue
        if not (in_allowed_network or host_listed):
            raise EgressPolicyError(
                f"host resolves to a private address ({addr}) outside this deployment's allowed "
                "database networks (CONNECTION_ALLOWED_NETWORKS)"
            )
