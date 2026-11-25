"""
Operational guardrails for outbound connections to customer-specified
destinations (connection profile test/discovery/sampling — see
app/services/connection_adapters.py and app/services/connection_discovery.py).

Letting a tenant declare an arbitrary hostname/port/URL for DataConnect to
connect to is the product's whole point, but it's also the classic SSRF
surface: whatever this backend can reach, a tenant can point it at.
`validate_destination()` is the one choke point every live network call in
those two modules goes through before any driver/HTTP call runs:

  - resolves the declared host and classifies every resolved address
    (loopback / link-local / private / reserved / public) using `ipaddress`;
  - blocks link-local (169.254.0.0/16, fe80::/10 — this is the cloud
    instance-metadata range on every major provider) and loopback
    unconditionally, unless explicitly turned off via settings (e.g. for
    local dev against 127.0.0.1) — there is no legitimate customer source at
    those addresses;
  - leaves general private-range (RFC1918/ULA) policy as a deployment
    choice (`settings.network_policy_block_private`), since a customer's own
    VPC-internal database living on a private address is the NORMAL case in
    a BYOC deployment, not an attack;
  - logs every decision — allowed or blocked — with the resolved
    classification, for audit.

`OutboundGuard` separately bounds how many of these connections can run at
once (global) and per organization per minute, so this capability can't be
turned into a port-scanning or resource-exhaustion primitive by looping
requests.

Known limitation: this validates the DNS answer at the time of the check: a
malicious DNS server could return a benign IP here and a different
(internal) IP moments later to the driver's own resolution (DNS rebinding).
Fully closing that gap means pinning the validated IP for the actual
connection in every driver (asyncpg, aiomysql, pytds, oracledb, pymongo,
ftplib, httpx) — out of scope for this pass. (Pinning the IP for the
httpx-based paths specifically was considered and rejected: rewriting the
request to the resolved IP breaks TLS certificate verification for every
HTTPS target, since the certificate is issued for the hostname, not the
IP — see app/core/http_probe.py.) What this still reliably closes is the
overwhelmingly common case: a destination that resolves — now, including
after any redirect — to a blocked range.
"""

import asyncio
import ipaddress
import socket
import time
from collections import defaultdict, deque
from dataclasses import dataclass

import structlog

from app.core.config import settings

logger = structlog.get_logger(__name__)

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


class DestinationBlockedError(RuntimeError):
    """Raised when a declared destination resolves to a network range this
    deployment's policy does not permit. Callers should surface this as a
    clear, distinct error — not a generic connection failure — so an
    operator can tell "policy blocked this" from "the source is down"."""


class RateLimitExceededError(RuntimeError):
    """Raised when an organization has exceeded the outbound connection
    probe rate limit."""


@dataclass(frozen=True)
class DestinationDecision:
    host: str
    resolved_ips: tuple[str, ...]
    classification: str  # loopback | link_local | private | reserved | public
    allowed: bool
    reason: str | None = None


def classify_ip(ip: IPAddress) -> str:
    if ip.is_loopback:
        return "loopback"
    if ip.is_link_local:
        return "link_local"
    if ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return "reserved"
    if ip.is_private:
        return "private"
    return "public"


_BLOCKED_BY_DEFAULT = {
    "loopback": lambda: settings.network_policy_block_loopback,
    "link_local": lambda: settings.network_policy_block_link_local,
    "reserved": lambda: settings.network_policy_block_reserved,
    "private": lambda: settings.network_policy_block_private,
}


async def _resolve(host: str) -> tuple[IPAddress, ...]:
    try:
        return (ipaddress.ip_address(host),)
    except ValueError:
        pass  # not a literal IP — go through DNS
    loop = asyncio.get_running_loop()
    infos = await asyncio.wait_for(
        loop.getaddrinfo(host, None, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM),
        timeout=settings.network_probe_connect_timeout_seconds,
    )
    addresses: list[IPAddress] = []
    seen: set[str] = set()
    for _family, _type, _proto, _canonname, sockaddr in infos:
        raw = sockaddr[0]
        if raw not in seen:
            seen.add(raw)
            addresses.append(ipaddress.ip_address(raw))
    return tuple(addresses)


async def validate_destination(
    host: str, *, org_id: str | None = None, purpose: str = "connection_probe"
) -> DestinationDecision:
    """Resolve `host` and confirm every address it resolves to is permitted
    by policy. Raises DestinationBlockedError (after logging the block) if
    not; returns the classified decision (and logs it) if allowed."""
    if not host:
        raise DestinationBlockedError("No destination host provided")

    try:
        resolved = await _resolve(host)
    except Exception as exc:
        logger.warning(
            "network_policy.resolve_failed", host=host, org_id=org_id, purpose=purpose, error=str(exc)
        )
        raise DestinationBlockedError(f"Could not resolve destination '{host}': {exc}") from exc

    if not resolved:
        raise DestinationBlockedError(f"Destination '{host}' did not resolve to any address")

    classifications = {classify_ip(ip) for ip in resolved}
    # Most restrictive resolved address decides — a hostname with even one
    # blocked address is blocked outright, since a client library may pick
    # any of the returned addresses to actually connect to.
    for label in ("link_local", "loopback", "reserved", "private"):
        if label in classifications and _BLOCKED_BY_DEFAULT[label]():
            decision = DestinationDecision(
                host=host, resolved_ips=tuple(str(ip) for ip in resolved),
                classification=label, allowed=False,
                reason=f"destination resolves to a blocked '{label}' address",
            )
            logger.warning(
                "network_policy.blocked", host=host, org_id=org_id, purpose=purpose,
                classification=label, resolved_ips=decision.resolved_ips,
            )
            raise DestinationBlockedError(
                f"Destination '{host}' resolves to a {label} address, which this "
                f"deployment's network policy does not permit."
            )

    classification = next(
        (c for c in ("link_local", "loopback", "reserved", "private") if c in classifications),
        "public",
    )
    decision = DestinationDecision(
        host=host, resolved_ips=tuple(str(ip) for ip in resolved),
        classification=classification, allowed=True,
    )
    logger.info(
        "network_policy.allowed", host=host, org_id=org_id, purpose=purpose,
        classification=classification, resolved_ips=decision.resolved_ips,
    )
    return decision


class OutboundGuard:
    """Bounds concurrent outbound connection probes globally and per org, so
    this capability can't become a port-scanning or resource-exhaustion
    primitive. In-process only (matches this codebase's other in-memory
    runtime state, e.g. InMemoryStore) — a multi-instance deployment wanting
    a hard global limit needs a shared backend (Redis) instead."""

    def __init__(self) -> None:
        self._semaphore = asyncio.Semaphore(settings.network_probe_max_concurrency)
        self._org_attempts: dict[str, deque] = defaultdict(deque)

    def _check_rate_limit(self, org_id: str) -> None:
        now = time.monotonic()
        attempts = self._org_attempts[org_id]
        while attempts and now - attempts[0] > 60.0:
            attempts.popleft()
        if len(attempts) >= settings.network_probe_max_per_org_per_minute:
            logger.warning("network_policy.rate_limited", org_id=org_id, attempts_last_minute=len(attempts))
            raise RateLimitExceededError(
                f"Too many connection probes for this organization "
                f"(limit: {settings.network_probe_max_per_org_per_minute}/minute). Try again shortly."
            )
        attempts.append(now)

    def guard(self, org_id: str | None):
        return _OutboundGuardContext(self, org_id or "unknown")


class _OutboundGuardContext:
    def __init__(self, owner: OutboundGuard, org_id: str):
        self._owner = owner
        self._org_id = org_id

    async def __aenter__(self):
        self._owner._check_rate_limit(self._org_id)
        await self._owner._semaphore.acquire()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self._owner._semaphore.release()
        return False


outbound_guard = OutboundGuard()
