import httpx
import pytest

from app.core.errors import KafkaConnectError
from app.services.runtime_orchestrator import KafkaConnectClient


@pytest.mark.asyncio
async def test_kafka_connect_client_routes_every_call_through_the_shared_client(monkeypatch) -> None:
    """KafkaConnectClient must not construct its own httpx.AsyncClient per
    call — every request goes through the shared, pooled client for its
    base_url (app.core.http_clients.get_client)."""
    call_count = {"get_client": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/connectors":
            return httpx.Response(200, json=["conn-a"])
        return httpx.Response(200, json={"connector": {"state": "RUNNING"}, "tasks": []})

    shared_client = httpx.AsyncClient(
        base_url="http://kafka-connect:8083", transport=httpx.MockTransport(handler)
    )

    async def _fake_get_client(base_url: str):
        call_count["get_client"] += 1
        assert base_url == "http://kafka-connect:8083"
        return shared_client

    monkeypatch.setattr("app.services.runtime_orchestrator.get_client", _fake_get_client)

    try:
        client = KafkaConnectClient("http://kafka-connect:8083")
        names = await client.list_connectors()
        status = await client.get_status("conn-a")

        assert names == ["conn-a"]
        assert status["connector"]["state"] == "RUNNING"
        # Two API calls, both funneled through get_client — never a client
        # built ad hoc inside a method.
        assert call_count["get_client"] == 2
    finally:
        await shared_client.aclose()


@pytest.mark.asyncio
async def test_kafka_connect_client_maps_connect_errors_to_http_exception(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"message": "Connector already exists"})

    shared_client = httpx.AsyncClient(
        base_url="http://kafka-connect:8083", transport=httpx.MockTransport(handler)
    )

    async def _fake_get_client(base_url: str):
        return shared_client

    monkeypatch.setattr("app.services.runtime_orchestrator.get_client", _fake_get_client)

    try:
        client = KafkaConnectClient("http://kafka-connect:8083")
        with pytest.raises(KafkaConnectError) as exc_info:
            await client.deploy_connector("conn-a", {"connector.class": "x"})

        assert exc_info.value.status_code == 422
        assert exc_info.value.retryable is False
        assert "Connector already exists" in exc_info.value.message
    finally:
        await shared_client.aclose()


@pytest.mark.asyncio
async def test_restart_restarts_the_tasks_too_not_only_the_connector_instance(monkeypatch) -> None:
    """FE-04, found against a real Kafka Connect: POST /restart with no query only restarts the connector instance (204) and leaves a FAILED task
    failed. The request must ask for the tasks too, otherwise "Restart" cannot recover a failed pipeline."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(202, json={"name": "c", "connector": {"state": "RESTARTING"}, "tasks": [{"id": 0, "state": "RESTARTING"}]})

    shared_client = httpx.AsyncClient(base_url="http://kafka-connect:8083", transport=httpx.MockTransport(handler))

    async def _fake_get_client(base_url: str):
        return shared_client

    monkeypatch.setattr("app.services.runtime_orchestrator.get_client", _fake_get_client)
    try:
        await KafkaConnectClient("http://kafka-connect:8083").restart_connector("c")
    finally:
        await shared_client.aclose()

    (request,) = seen
    assert request.method == "POST"
    assert request.url.path == "/connectors/c/restart"
    assert dict(request.url.params) == {"includeTasks": "true", "onlyFailed": "false"}


@pytest.mark.asyncio
async def test_a_rejected_restart_is_still_an_error(monkeypatch) -> None:
    shared_client = httpx.AsyncClient(
        base_url="http://kafka-connect:8083", transport=httpx.MockTransport(lambda r: httpx.Response(404, json={"message": "Connector c not found"}))
    )

    async def _fake_get_client(base_url: str):
        return shared_client

    monkeypatch.setattr("app.services.runtime_orchestrator.get_client", _fake_get_client)
    try:
        with pytest.raises(KafkaConnectError):
            await KafkaConnectClient("http://kafka-connect:8083").restart_connector("c")
    finally:
        await shared_client.aclose()
