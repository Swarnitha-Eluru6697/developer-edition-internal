"""Which connector templates can actually run, as machine-readable availability (NEW-09; see app/core/capability.py).

The catalog lists every connector a template describes — 128 of them — but a template only works if its connector plugin is installed on the Kafka
Connect worker that would run it. Most are not, in a typical deployment. Without this, the catalog offered them all and the user found out at deploy
time, with a preflight error, that a plugin was missing.

Availability is decided from the same checks the deploy preflight makes (deploy_preflight.connector_plugin_finding / worker_version_finding), so the
catalog and a deploy cannot disagree about a plugin or a version. Each distinct Kafka Connect cluster is asked once, in parallel, and its answer is
reused for settings.connector_availability_ttl_seconds.

    template switched off, or no installable connector class           -> unavailable
    plugin missing / wrong kind / wrong version, or an unsupported worker -> unavailable
    the Connect cluster could not be reached                           -> requires_configuration, verified=false (never "supported": nobody confirmed it)
    otherwise                                                          -> supported
"""

import asyncio
import re
from dataclasses import dataclass
from typing import Any

import structlog

from app.core.capability import (
    NO_CONNECTOR_CLASS,
    RUNTIME_UNREACHABLE,
    SUPPORTED,
    TEMPLATE_INACTIVE,
    UNKNOWN_POOL,
    Availability,
    requires_configuration,
    unavailable,
)
from app.core.config import settings
from app.services.connect_runtime import client_for_pool, pool_endpoint, pool_for_template
from app.services.deploy_preflight import (
    CachedPluginClient,
    connector_plugin_finding,
    worker_version_finding,
)

logger = structlog.get_logger(__name__)

_REMEDIATION = {
    "PLUGIN_NOT_INSTALLED": "Install the connector plugin on the Kafka Connect worker.",
    "PLUGIN_WRONG_KIND": "Use the template for the kind of connector that is installed, or install the matching plugin.",
    "PLUGIN_VERSION_INCOMPATIBLE": "Upgrade the plugin, or use a template qualified for the installed version.",
    "WORKER_VERSION_UNSUPPORTED": "Run a Kafka Connect version this template was qualified for.",
}
# A catalog entry whose "connector class" is a note ("Contact Onibex for connector class") rather than a Java class name cannot be installed at all.
_JAVA_CLASS = re.compile(r"^[A-Za-z_$][\w$]*(\.[A-Za-z_$][\w$]*)*$")

_clients: dict[str, CachedPluginClient] = {}      # one cached client per Kafka Connect endpoint


@dataclass(frozen=True)
class RuntimeSnapshot:
    """What one Kafka Connect cluster reported. `error` is set (and plugins is None) when it could not be asked."""

    plugins: list[dict[str, Any]] | None
    worker: dict[str, Any] | None
    error: str | None = None


def _client_for(pool_id: str) -> CachedPluginClient:
    endpoint = pool_endpoint(pool_id)
    client = _clients.get(endpoint)
    if client is None:
        client = _clients[endpoint] = CachedPluginClient(client_for_pool(pool_id), ttl_seconds=settings.connector_availability_ttl_seconds)
    return client


def reset_cache() -> None:
    """Forget every cluster's cached answer (tests, and an operator who has just installed a plugin and does not want to wait out the TTL)."""
    _clients.clear()


async def _query_cluster(pool_id: str, need_worker: bool) -> RuntimeSnapshot:
    client = _client_for(pool_id)
    try:
        async with asyncio.timeout(settings.connector_availability_timeout_seconds):
            plugins = await client.list_connector_plugins()
            worker = await client.get_worker_info() if need_worker else None
    except Exception as exc:  # noqa: BLE001 - any failure means "could not be asked"; the class name is all that leaves this function
        logger.warning("connector_availability.runtime_unreachable", pool=pool_id, error=type(exc).__name__)
        return RuntimeSnapshot(None, None, error=type(exc).__name__)
    return RuntimeSnapshot(plugins, worker)


# The one place the catalog asks a cluster anything. Kept as its own name so a test can substitute canned answers without touching the query itself.
_snapshot = _query_cluster


def template_availability(template: dict[str, Any], snapshot: RuntimeSnapshot | None) -> Availability:
    """The availability of ONE template given what its cluster reported. Pure: no I/O, so every branch is testable. `snapshot` is None only when
    the template's worker pool does not exist."""
    if template.get("is_active") is False:
        return unavailable(TEMPLATE_INACTIVE, "This connector template has been switched off.", "Ask a catalog administrator to activate it.")
    connector_class = str(template.get("connector_class") or "")
    if not _JAVA_CLASS.fullmatch(connector_class):
        return unavailable(
            NO_CONNECTOR_CLASS, f"This catalog entry has no installable Kafka Connect connector class ({connector_class or 'none'!r}).",
            "It is a placeholder; use a connector that has a plugin.",
        )
    if snapshot is None:
        return unavailable(UNKNOWN_POOL, f"This template names a worker pool that does not exist ({template.get('runtime_pool_type')!r}).")
    if snapshot.plugins is None:
        return requires_configuration(
            RUNTIME_UNREACHABLE, "Kafka Connect could not be reached, so whether this connector can run could not be confirmed.",
            "Check that Kafka Connect is running and DATACONNECT_KAFKA_CONNECT_URL (or DATACONNECT_CONNECT_POOLS) points at it.", verified=False,
        )
    finding = connector_plugin_finding(template, snapshot.plugins)
    if finding is None and snapshot.worker is not None:
        finding = worker_version_finding(template, snapshot.worker)
    if finding is not None:
        _check, reason, code = finding
        return unavailable(code, reason, _REMEDIATION.get(code))
    return SUPPORTED


async def availability_for_templates(templates: list[dict[str, Any]]) -> dict[str, Availability]:
    """{template id: Availability}. Each distinct Kafka Connect cluster is asked once, concurrently, however many templates run on it."""
    pool_of: dict[str, str | None] = {}
    for template in templates:
        try:
            pool_of[template["id"]] = pool_for_template(template)
        except ValueError:
            pool_of[template["id"]] = None

    by_endpoint: dict[str, tuple[str, bool]] = {}            # endpoint -> (a pool on it, does any template on it pin worker versions)
    for template in templates:
        pool = pool_of[template["id"]]
        if pool is None:
            continue
        endpoint = pool_endpoint(pool)
        first_pool, need_worker = by_endpoint.get(endpoint, (pool, False))
        by_endpoint[endpoint] = (first_pool, need_worker or bool(template.get("supported_kafka_connect_versions")))

    endpoints = list(by_endpoint)
    snapshots = await asyncio.gather(*(_snapshot(by_endpoint[e][0], by_endpoint[e][1]) for e in endpoints))
    by_url = dict(zip(endpoints, snapshots, strict=True))

    result: dict[str, Availability] = {}
    for template in templates:
        pool = pool_of[template["id"]]
        result[template["id"]] = template_availability(template, by_url[pool_endpoint(pool)] if pool is not None else None)
    return result


async def annotate(templates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The templates with an `availability` block added (copies; the inputs are untouched)."""
    availability = await availability_for_templates(templates)
    return [{**template, "availability": availability[template["id"]].as_dict()} for template in templates]
