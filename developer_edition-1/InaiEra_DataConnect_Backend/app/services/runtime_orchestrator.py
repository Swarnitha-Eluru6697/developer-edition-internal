from typing import Any

import httpx

from app.core.errors import KafkaConnectConfigError, KafkaConnectError
from app.core.http_clients import get_client
from app.services.secrets import redact_connector_config


def _raise_for_connect(response: httpx.Response) -> None:
    """Turn a Kafka Connect error response into the right exception — see
    KafkaConnectError/KafkaConnectConfigError in app/core/errors.py for why
    these are two distinct codes (FE-BUG-04): a 400/409 means Connect was
    reached and reviewed the request but rejected the connector config
    itself (KAFKA_CONNECT_CONFIG_INVALID, e.g. "Replication slot already
    exists"), while anything else is Connect failing or being unreachable
    (KAFKA_CONNECT_UNAVAILABLE) — different next steps for a caller (fix the
    config vs. wait and retry), so different codes even though both come
    from this one response."""
    if not response.is_error:
        return
    try:
        body = response.json()
        msg = body.get("message") if isinstance(body, dict) else body
    except Exception:
        msg = response.text
    msg = msg or response.reason_phrase
    error_cls = KafkaConnectConfigError if response.status_code in (400, 409) else KafkaConnectError
    raise error_cls(
        f"Kafka Connect error ({response.status_code}): {msg}",
        details={"connect_status_code": response.status_code},
    )


class KafkaConnectClient:
    """Thin, cheap-to-construct wrapper — the actual httpx.AsyncClient (with
    its connection pool) is shared across every KafkaConnectClient instance
    pointed at the same base_url, via app.core.http_clients. Constructing one
    of these does not open a connection; only issuing a request does, and it
    reuses the pool rather than opening a new one each time."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    async def _client(self) -> httpx.AsyncClient:
        return await get_client(self.base_url)

    async def list_connectors(self) -> list[str]:
        client = await self._client()
        response = await client.get("/connectors")
        _raise_for_connect(response)
        return response.json()

    async def list_connector_plugins(self) -> list[dict[str, Any]]:
        """Every plugin the worker actually has installed — connectors, SMTs
        (transformation), converters, header converters and predicates alike
        (KIP-769, Kafka Connect 3.5+; `connectorsOnly=false` is what makes it
        return the non-connector kinds too, not just source/sink connectors).
        Used by app/services/deploy_preflight.py to verify a pipeline's
        connector class, SMTs and converters are really installed BEFORE
        deploy_connector — Connect has no REST endpoint for a worker's
        configured ConfigProviders, so that one check can't use this."""
        client = await self._client()
        response = await client.get("/connector-plugins", params={"connectorsOnly": "false"})
        _raise_for_connect(response)
        return response.json()

    async def get_worker_info(self) -> dict[str, Any]:
        """The root endpoint — carries the Connect worker's own version, used
        to check a template's supported_kafka_connect_versions."""
        client = await self._client()
        response = await client.get("/")
        _raise_for_connect(response)
        return response.json()

    async def get_status(self, connector_name: str) -> dict[str, Any]:
        client = await self._client()
        response = await client.get(f"/connectors/{connector_name}/status")
        _raise_for_connect(response)
        return response.json()

    async def deploy_connector(self, connector_name: str, config: dict[str, Any]) -> dict[str, Any]:
        client = await self._client()
        response = await client.put(f"/connectors/{connector_name}/config", json=config, timeout=30)
        _raise_for_connect(response)
        return response.json()

    async def pause_connector(self, connector_name: str) -> None:
        client = await self._client()
        response = await client.put(f"/connectors/{connector_name}/pause")
        _raise_for_connect(response)

    async def resume_connector(self, connector_name: str) -> None:
        client = await self._client()
        response = await client.put(f"/connectors/{connector_name}/resume")
        _raise_for_connect(response)

    async def restart_connector(self, connector_name: str) -> None:
        """Restart the connector AND its tasks. Without `includeTasks=true` Kafka Connect restarts only the connector instance (a 204 that looks like
        success) and leaves a FAILED task failed — so a "restart" could never recover the one case people use it for (found by the live UI E2E, FE-04).
        `onlyFailed=false` restarts every task, so a task that is merely stuck is refreshed too."""
        client = await self._client()
        response = await client.post(
            f"/connectors/{connector_name}/restart", params={"includeTasks": "true", "onlyFailed": "false"}
        )
        _raise_for_connect(response)

    async def delete_connector(self, connector_name: str) -> None:
        client = await self._client()
        response = await client.delete(f"/connectors/{connector_name}")
        if response.status_code == 404:
            # DELETE is idempotent by definition: the connector already being
            # gone — whether a previous attempt actually succeeded and only
            # its response was lost, or someone else removed it — IS the
            # desired outcome, not a failure.
            return
        _raise_for_connect(response)

    async def stop_connector(self, connector_name: str) -> None:
        """KIP-875 (Kafka Connect 3.5+): fully shuts the connector's tasks
        down into a STOPPED state — distinct from pause. Offsets can only be
        deleted from a connector that still exists and is STOPPED, so this
        must run before delete_connector_offsets, not after."""
        client = await self._client()
        response = await client.put(f"/connectors/{connector_name}/stop")
        if response.status_code == 404:
            return
        _raise_for_connect(response)

    async def delete_connector_offsets(self, connector_name: str) -> None:
        """KIP-875 (Kafka Connect 3.6+): requires the connector to still
        exist and be STOPPED (404 if it's already gone, 400 if it isn't
        stopped) — must run after stop_connector and before
        delete_connector."""
        client = await self._client()
        response = await client.delete(f"/connectors/{connector_name}/offsets")
        if response.status_code == 404:
            return
        _raise_for_connect(response)


class RuntimeOrchestratorService:
    def __init__(self, client: KafkaConnectClient):
        self.client = client

    async def deploy_pipeline(self, pipeline: dict[str, Any]) -> dict[str, Any]:
        config = dict(pipeline["generated_config"])
        config.pop("name", None)
        deployed = await self.client.deploy_connector(pipeline["connector_name"], config)
        # Kafka Connect echoes the config we just PUT — including real credentials —
        # back in its response body; redact before it's returned to any caller.
        if isinstance(deployed, dict) and isinstance(deployed.get("config"), dict):
            deployed = {**deployed, "config": redact_connector_config(deployed["config"])}
        return {
            "pipeline_id": pipeline["id"],
            "connector_name": pipeline["connector_name"],
            # Reflects only that the request was issued, not that Kafka
            # Connect has confirmed it — the runtime reconciler owns the
            # confirmed/observed state (see runtime_reconciler.py).
            "status": "deploying",
            "runtime_response": deployed,
        }

    async def pause_pipeline(self, pipeline: dict[str, Any]) -> dict[str, Any]:
        await self.client.pause_connector(pipeline["connector_name"])
        return {"pipeline_id": pipeline["id"], "connector_name": pipeline["connector_name"], "status": "pausing"}

    async def resume_pipeline(self, pipeline: dict[str, Any]) -> dict[str, Any]:
        await self.client.resume_connector(pipeline["connector_name"])
        return {"pipeline_id": pipeline["id"], "connector_name": pipeline["connector_name"], "status": "resuming"}

    async def restart_pipeline(self, pipeline: dict[str, Any]) -> dict[str, Any]:
        await self.client.restart_connector(pipeline["connector_name"])
        return {
            "pipeline_id": pipeline["id"],
            "connector_name": pipeline["connector_name"],
            "status": "restarting",
        }

    async def connector_status(self, connector_name: str) -> dict[str, Any]:
        return await self.client.get_status(connector_name)
