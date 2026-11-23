import asyncio
import ipaddress

import pytest

from app.core.config import settings
from app.core.network_policy import (
    DestinationBlockedError,
    OutboundGuard,
    RateLimitExceededError,
    classify_ip,
    validate_destination,
)


def test_classify_ip_loopback() -> None:
    assert classify_ip(ipaddress.ip_address("127.0.0.1")) == "loopback"
    assert classify_ip(ipaddress.ip_address("::1")) == "loopback"


def test_classify_ip_link_local_covers_cloud_metadata() -> None:
    # 169.254.169.254 is the cloud instance-metadata address on AWS/GCP/Azure.
    assert classify_ip(ipaddress.ip_address("169.254.169.254")) == "link_local"
    assert classify_ip(ipaddress.ip_address("fe80::1")) == "link_local"


def test_classify_ip_private() -> None:
    assert classify_ip(ipaddress.ip_address("10.0.0.5")) == "private"
    assert classify_ip(ipaddress.ip_address("192.168.1.1")) == "private"
    assert classify_ip(ipaddress.ip_address("172.16.0.1")) == "private"


def test_classify_ip_public() -> None:
    assert classify_ip(ipaddress.ip_address("8.8.8.8")) == "public"


def test_classify_ip_reserved() -> None:
    assert classify_ip(ipaddress.ip_address("0.0.0.0")) == "reserved"
    assert classify_ip(ipaddress.ip_address("224.0.0.1")) == "reserved"  # multicast


@pytest.mark.asyncio
async def test_validate_destination_blocks_cloud_metadata_address() -> None:
    with pytest.raises(DestinationBlockedError):
        await validate_destination("169.254.169.254")


@pytest.mark.asyncio
async def test_validate_destination_blocks_loopback_by_default() -> None:
    with pytest.raises(DestinationBlockedError):
        await validate_destination("127.0.0.1")


@pytest.mark.asyncio
async def test_validate_destination_allows_private_by_default() -> None:
    """A customer's own VPC-internal database is the normal case, not an
    attack — private ranges are policy-configurable, not hard-blocked."""
    decision = await validate_destination("10.0.0.5")
    assert decision.allowed is True
    assert decision.classification == "private"


@pytest.mark.asyncio
async def test_validate_destination_allows_public_address() -> None:
    decision = await validate_destination("8.8.8.8")
    assert decision.allowed is True
    assert decision.classification == "public"


@pytest.mark.asyncio
async def test_validate_destination_can_block_private_ranges_via_settings(monkeypatch) -> None:
    monkeypatch.setattr(settings, "network_policy_block_private", True)
    with pytest.raises(DestinationBlockedError):
        await validate_destination("10.0.0.5")


@pytest.mark.asyncio
async def test_validate_destination_loopback_can_be_allowed_via_settings(monkeypatch) -> None:
    monkeypatch.setattr(settings, "network_policy_block_loopback", False)
    decision = await validate_destination("127.0.0.1")
    assert decision.allowed is True
    assert decision.classification == "loopback"


@pytest.mark.asyncio
async def test_validate_destination_rejects_empty_host() -> None:
    with pytest.raises(DestinationBlockedError):
        await validate_destination("")


@pytest.mark.asyncio
async def test_validate_destination_resolves_hostname_and_blocks_loopback() -> None:
    """Not just literal IPs — a hostname that resolves to loopback must be
    blocked the same way. "localhost" is the textbook example, and resolves
    via the OS hosts file / stub resolver without needing real network access."""
    with pytest.raises(DestinationBlockedError):
        await validate_destination("localhost")


@pytest.mark.asyncio
async def test_outbound_guard_enforces_per_org_rate_limit(monkeypatch) -> None:
    monkeypatch.setattr(settings, "network_probe_max_per_org_per_minute", 2)
    guard = OutboundGuard()

    async with guard.guard("org_rate_test"):
        pass
    async with guard.guard("org_rate_test"):
        pass

    with pytest.raises(RateLimitExceededError):
        async with guard.guard("org_rate_test"):
            pass


@pytest.mark.asyncio
async def test_outbound_guard_tracks_orgs_independently(monkeypatch) -> None:
    monkeypatch.setattr(settings, "network_probe_max_per_org_per_minute", 1)
    guard = OutboundGuard()

    async with guard.guard("org_a"):
        pass
    # A different org's own budget must be unaffected.
    async with guard.guard("org_b"):
        pass


@pytest.mark.asyncio
async def test_outbound_guard_limits_global_concurrency(monkeypatch) -> None:
    monkeypatch.setattr(settings, "network_probe_max_concurrency", 1)
    guard = OutboundGuard()
    started = asyncio.Event()
    release = asyncio.Event()

    async def _hold():
        async with guard.guard("org_conc_1"):
            started.set()
            await release.wait()

    task = asyncio.create_task(_hold())
    await started.wait()

    async def _try_second():
        async with guard.guard("org_conc_2"):
            return "acquired"

    # A second, DIFFERENT org's probe must still wait for the GLOBAL
    # semaphore rather than running in parallel with the first.
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(_try_second(), timeout=0.2)

    release.set()
    await task
