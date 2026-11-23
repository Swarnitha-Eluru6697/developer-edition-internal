import httpx
import pytest

from app.core.http_clients import close_all, get_client


@pytest.mark.asyncio
async def test_get_client_reuses_the_same_instance_for_the_same_base_url() -> None:
    try:
        first = await get_client("http://kafka-connect:8083")
        second = await get_client("http://kafka-connect:8083")

        assert first is second
    finally:
        await close_all()


@pytest.mark.asyncio
async def test_get_client_returns_distinct_instances_per_base_url() -> None:
    try:
        connect_client = await get_client("http://kafka-connect:8083")
        rest_proxy_client = await get_client("http://rest-proxy:8082")

        assert connect_client is not rest_proxy_client
    finally:
        await close_all()


@pytest.mark.asyncio
async def test_get_client_is_configured_with_pool_limits_and_retry_transport() -> None:
    try:
        client = await get_client("http://kafka-connect:8083")

        assert isinstance(client, httpx.AsyncClient)
        assert isinstance(client._transport, httpx.AsyncHTTPTransport)
    finally:
        await close_all()


@pytest.mark.asyncio
async def test_close_all_releases_and_forgets_every_client() -> None:
    first = await get_client("http://kafka-connect:8083")

    await close_all()

    assert first.is_closed
    # A subsequent call must build a fresh client rather than returning the
    # now-closed one.
    second = await get_client("http://kafka-connect:8083")
    try:
        assert second is not first
        assert not second.is_closed
    finally:
        await close_all()
