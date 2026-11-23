import httpx
import pytest

from app.core.errors import KafkaConnectError
from app.services.kafka_connect_retry import (
    delete_converged,
    deploy_if_current,
    get_status_retryable,
    list_connectors_retryable,
    pause_if_needed,
    restart_once,
    resume_if_needed,
)
from app.services.runtime_orchestrator import KafkaConnectClient


def _client(handler) -> KafkaConnectClient:
    """A KafkaConnectClient whose calls are served by `handler` instead of a
    real network — routed through a fresh, isolated shared client via
    app.core.http_clients so tests never touch each other's pooled state."""
    shared = httpx.AsyncClient(base_url="http://kafka-connect:8083", transport=httpx.MockTransport(handler))
    client = KafkaConnectClient("http://kafka-connect:8083")
    client._client = lambda: _identity_awaitable(shared)  # bypass the real pool for this test
    return client, shared


async def _identity_awaitable(value):
    return value


async def _always_true() -> bool:
    return True


async def _always_false() -> bool:
    return False


@pytest.mark.asyncio
async def test_get_status_retryable_retries_transient_failure_then_succeeds() -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] < 3:
            return httpx.Response(503, text="unavailable")
        return httpx.Response(200, json={"connector": {"state": "RUNNING"}, "tasks": []})

    client, shared = _client(handler)
    try:
        status = await get_status_retryable(client, "conn-a")
        assert status["connector"]["state"] == "RUNNING"
        assert calls["count"] == 3
    finally:
        await shared.aclose()


@pytest.mark.asyncio
async def test_get_status_retryable_gives_up_after_max_attempts() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="unavailable")

    client, shared = _client(handler)
    try:
        with pytest.raises(KafkaConnectError):
            await get_status_retryable(client, "conn-a")
    finally:
        await shared.aclose()


@pytest.mark.asyncio
async def test_list_connectors_retryable_recovers_from_one_failure() -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(200, json=["conn-a"])

    client, shared = _client(handler)
    try:
        names = await list_connectors_retryable(client)
        assert names == ["conn-a"]
    finally:
        await shared.aclose()


@pytest.mark.asyncio
async def test_deploy_if_current_applies_when_still_current() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"config": {}})

    client, shared = _client(handler)
    try:
        result = await deploy_if_current(
            client, "conn-a", {"tasks.max": "1"}, still_current=_always_true
        )
        assert result == {"config": {}}
    finally:
        await shared.aclose()


@pytest.mark.asyncio
async def test_deploy_if_current_backs_off_when_superseded() -> None:
    """A stale retry must never apply a config over a newer desired state —
    it should just quietly give up, not error and not deploy."""
    called = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        called["count"] += 1
        return httpx.Response(200, json={"config": {}})

    client, shared = _client(handler)
    try:
        result = await deploy_if_current(
            client, "conn-a", {"tasks.max": "1"}, still_current=_always_false
        )
        assert result is None
        assert called["count"] == 0
    finally:
        await shared.aclose()


@pytest.mark.asyncio
async def test_deploy_if_current_rechecks_currency_between_retries() -> None:
    """The generation check must run before EVERY attempt, not just the
    first — a config that stops being current mid-retry must stop applying."""
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        return httpx.Response(503, text="unavailable")

    current_checks = {"count": 0}

    async def _flaky_current() -> bool:
        current_checks["count"] += 1
        return current_checks["count"] <= 1  # current for the first attempt only

    client, shared = _client(handler)
    try:
        result = await deploy_if_current(
            client, "conn-a", {"tasks.max": "1"}, still_current=_flaky_current, backoff_seconds=0.001
        )
        assert result is None
        assert attempts["count"] == 1  # only the first (failed) attempt was ever tried
    finally:
        await shared.aclose()


@pytest.mark.asyncio
async def test_pause_if_needed_skips_when_already_paused() -> None:
    calls = {"pause": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"connector": {"state": "PAUSED"}, "tasks": []})
        calls["pause"] += 1
        return httpx.Response(202)

    client, shared = _client(handler)
    try:
        made_call = await pause_if_needed(client, "conn-a")
        assert made_call is False
        assert calls["pause"] == 0
    finally:
        await shared.aclose()


@pytest.mark.asyncio
async def test_pause_if_needed_pauses_when_not_yet_paused() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"connector": {"state": "RUNNING"}, "tasks": []})
        return httpx.Response(202)

    client, shared = _client(handler)
    try:
        made_call = await pause_if_needed(client, "conn-a")
        assert made_call is True
    finally:
        await shared.aclose()


@pytest.mark.asyncio
async def test_resume_if_needed_skips_when_already_running() -> None:
    calls = {"resume": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"connector": {"state": "RUNNING"}, "tasks": []})
        calls["resume"] += 1
        return httpx.Response(202)

    client, shared = _client(handler)
    try:
        made_call = await resume_if_needed(client, "conn-a")
        assert made_call is False
        assert calls["resume"] == 0
    finally:
        await shared.aclose()


@pytest.mark.asyncio
async def test_restart_once_never_retries() -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(503, text="unavailable")

    client, shared = _client(handler)
    try:
        with pytest.raises(KafkaConnectError):
            await restart_once(client, "conn-a")
        assert calls["count"] == 1
    finally:
        await shared.aclose()


@pytest.mark.asyncio
async def test_delete_converged_treats_404_as_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "not found"})

    client, shared = _client(handler)
    try:
        await delete_converged(client, "conn-a")  # must not raise
    finally:
        await shared.aclose()


@pytest.mark.asyncio
async def test_delete_converged_retries_transient_failure() -> None:
    """delete_converged makes three distinct calls in sequence — stop the
    connector, delete its offsets, then delete the connector itself (see
    that function's docstring for why offset deletion must happen while the
    connector still exists and is stopped) — so the retry behavior is
    asserted per-endpoint, not as one combined count: the connector delete
    retries once on a transient 503 before succeeding, while stop/offsets
    each succeed on the first try."""
    calls = {"stop": 0, "delete_offsets": 0, "delete_connector": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/stop"):
            calls["stop"] += 1
            return httpx.Response(204)
        if request.url.path.endswith("/offsets"):
            calls["delete_offsets"] += 1
            return httpx.Response(200, json={"message": "ok"})
        calls["delete_connector"] += 1
        if calls["delete_connector"] < 2:
            return httpx.Response(503, text="unavailable")
        return httpx.Response(204)

    client, shared = _client(handler)
    try:
        await delete_converged(client, "conn-a")
        assert calls["stop"] == 1
        assert calls["delete_offsets"] == 1
        assert calls["delete_connector"] == 2
    finally:
        await shared.aclose()


@pytest.mark.asyncio
async def test_a_422_from_connect_is_never_retried() -> None:
    """A definite rejection (invalid/conflicting request) will fail again
    identically — retrying it is pointless and just delays the real error."""
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(400, json={"message": "invalid connector config"})

    client, shared = _client(handler)
    try:
        with pytest.raises(KafkaConnectError):
            await get_status_retryable(client, "conn-a")
        assert calls["count"] == 1
    finally:
        await shared.aclose()
