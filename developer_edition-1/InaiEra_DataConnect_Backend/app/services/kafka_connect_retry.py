"""
Kafka Connect retry / idempotency policy.

KafkaConnectClient (runtime_orchestrator.py) is a raw, context-free HTTP
transport — it has no notion of desired state, generations, or what
"already converged" means, so it has no basis to decide on its own whether
a failed call is safe to retry. This module is where that decision is made
explicit, one rule per operation. It's the runtime reconciler
(runtime_reconciler.py) that supplies the context each rule needs
(desired_generation, current observed state).

Blind retries are unsafe for some of these operations — an unconditioned
retry of a stale deploy could overwrite a newer desired config with an
older one; retrying a restart against a connector that already came back up
on its own just flaps it — and no retries at all make an ordinary transient
network blip unnecessarily disruptive. The rules:

  GET (list_connectors / get_status)
      Always safe to retry — read-only, no side effects.

  PUT connector config (deploy)
      Idempotent at the HTTP level (same body -> same result), but only
      safe to retry while it's still actually wanted: `deploy_if_current`
      re-checks the caller's `still_current` predicate before every attempt
      (including the first) and gives up cleanly — no exception, no partial
      apply — the moment a newer desired state has superseded this one.

  pause / resume
      `pause_if_needed` / `resume_if_needed` check Kafka Connect's actual
      current state before every attempt; if it already matches the target,
      no call is made at all (nothing to do, and nothing to retry). A retry
      after a failed attempt re-checks state again rather than blindly
      reissuing the same call.

  restart
      `restart_once` is exactly one attempt, deliberately not retried here.
      A failed restart is left for the next reconcile tick, which
      re-observes real state before deciding whether a restart is still
      needed — retrying it immediately risks flapping a connector that
      already recovered on its own.

  delete
      `delete_converged` retries transient failures; KafkaConnectClient's
      delete_connector already treats a 404 (connector not found) as
      success rather than an error, so this rule falls out of the same
      generic "retry on transient failure" logic as GET.

A definite, non-transient rejection from Kafka Connect (HTTP 400/409,
surfaced by KafkaConnectClient as a 422) is never retried anywhere in this
module — retrying an invalid config or a conflicting request will not
change the outcome.
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import structlog

from app.core.errors import KafkaConnectError
from app.services.runtime_orchestrator import KafkaConnectClient

logger = structlog.get_logger(__name__)

T = TypeVar("T")

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_SECONDS = 0.5


def _is_retryable(exc: Exception) -> bool:
    """False for a definite rejection from Kafka Connect itself (its own
    `retryable=False` — a 422 config/conflict error, see
    KafkaConnectClient._raise_for_connect); True for everything else
    (transport failures, and Connect's own 502-mapped upstream errors),
    since those may well be transient."""
    if isinstance(exc, KafkaConnectError):
        return exc.retryable
    return True


async def _retry(
    factory: Callable[[], Awaitable[T]],
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
    operation: str = "kafka_connect_call",
) -> T:
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await factory()
        except Exception as exc:
            if not _is_retryable(exc):
                raise
            last_exc = exc
            logger.warning(
                "kafka_connect.retry", operation=operation, attempt=attempt,
                max_attempts=max_attempts, error=str(exc),
            )
        if attempt < max_attempts:
            await asyncio.sleep(backoff_seconds * (2 ** (attempt - 1)))
    assert last_exc is not None
    raise last_exc


async def list_connectors_retryable(client: KafkaConnectClient) -> list[str]:
    """GET — always safe to retry."""
    return await _retry(client.list_connectors, operation="list_connectors")


async def get_status_retryable(client: KafkaConnectClient, connector_name: str) -> dict[str, Any]:
    """GET — always safe to retry."""
    return await _retry(
        lambda: client.get_status(connector_name), operation="get_status"
    )


async def deploy_if_current(
    client: KafkaConnectClient,
    connector_name: str,
    config: dict[str, Any],
    *,
    still_current: Callable[[], Awaitable[bool]],
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
) -> dict[str, Any] | None:
    """PUT the connector config, retrying transient failures — but only
    while `still_current()` keeps confirming this is the config we actually
    want applied. Returns None (not an error — nothing to report, the next
    tick will apply whatever superseded it) the moment it isn't."""
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        if not await still_current():
            logger.info(
                "kafka_connect.deploy_superseded", connector=connector_name, attempt=attempt
            )
            return None
        try:
            return await client.deploy_connector(connector_name, config)
        except Exception as exc:
            if not _is_retryable(exc):
                raise
            last_exc = exc
            logger.warning(
                "kafka_connect.retry", operation="deploy_connector",
                connector=connector_name, attempt=attempt, max_attempts=max_attempts, error=str(exc),
            )
        if attempt < max_attempts:
            await asyncio.sleep(backoff_seconds * (2 ** (attempt - 1)))
    assert last_exc is not None
    raise last_exc


async def _toggle_if_needed(
    client: KafkaConnectClient,
    connector_name: str,
    *,
    target_state: str,
    action: Callable[[], Awaitable[None]],
    operation: str,
    max_attempts: int,
    backoff_seconds: float,
) -> bool:
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            status = await client.get_status(connector_name)
        except Exception as exc:
            if not _is_retryable(exc):
                raise
            last_exc = exc
            logger.warning(
                "kafka_connect.retry", operation=f"{operation}.status_check",
                connector=connector_name, attempt=attempt, max_attempts=max_attempts, error=str(exc),
            )
            if attempt < max_attempts:
                await asyncio.sleep(backoff_seconds * (2 ** (attempt - 1)))
            continue

        if (status.get("connector") or {}).get("state") == target_state:
            return False  # already there — nothing to do, nothing to retry

        try:
            await action()
            return True
        except Exception as exc:
            if not _is_retryable(exc):
                raise
            last_exc = exc
            logger.warning(
                "kafka_connect.retry", operation=operation,
                connector=connector_name, attempt=attempt, max_attempts=max_attempts, error=str(exc),
            )
        if attempt < max_attempts:
            await asyncio.sleep(backoff_seconds * (2 ** (attempt - 1)))
    assert last_exc is not None
    raise last_exc


async def pause_if_needed(
    client: KafkaConnectClient,
    connector_name: str,
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
) -> bool:
    """Pause only if Kafka Connect doesn't already report PAUSED — checked
    fresh before every attempt, so a retry never re-issues a pause the first
    attempt actually already achieved. Returns True iff a call was made."""
    return await _toggle_if_needed(
        client, connector_name, target_state="PAUSED", action=lambda: client.pause_connector(connector_name),
        operation="pause_connector", max_attempts=max_attempts, backoff_seconds=backoff_seconds,
    )


async def resume_if_needed(
    client: KafkaConnectClient,
    connector_name: str,
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
) -> bool:
    """Resume only if Kafka Connect doesn't already report RUNNING."""
    return await _toggle_if_needed(
        client, connector_name, target_state="RUNNING", action=lambda: client.resume_connector(connector_name),
        operation="resume_connector", max_attempts=max_attempts, backoff_seconds=backoff_seconds,
    )


async def restart_once(client: KafkaConnectClient, connector_name: str) -> None:
    """Exactly one attempt — deliberately not retried. A failed restart is
    left for the next reconcile tick, which re-observes real state before
    deciding whether a restart is still needed."""
    await client.restart_connector(connector_name)


async def delete_converged(
    client: KafkaConnectClient,
    connector_name: str,
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
) -> None:
    """Delete, retrying transient failures. Before the actual delete, best-effort
    stop the connector and clear its offsets (Kafka Connect 3.5+/3.6+, KIP-875)
    so that if a pipeline with the exact same name is created later, it starts
    fresh rather than resuming from stale committed offsets. Both must run
    BEFORE delete_connector: offset deletion requires the connector to still
    exist and be in the STOPPED state, so it's impossible to do this the other
    way around. Failures here are only logged, never block the real delete —
    a connector that no longer exists is the actual desired outcome regardless
    of whether its offsets were cleared."""
    try:
        await _retry(
            lambda: client.stop_connector(connector_name),
            max_attempts=max_attempts, backoff_seconds=backoff_seconds, operation="stop_connector",
        )
        await _retry(
            lambda: client.delete_connector_offsets(connector_name),
            max_attempts=max_attempts, backoff_seconds=backoff_seconds, operation="delete_connector_offsets",
        )
    except Exception as exc:
        logger.warning("kafka_connect.delete_offsets_failed", connector=connector_name, error=str(exc))

    await _retry(
        lambda: client.delete_connector(connector_name),
        max_attempts=max_attempts, backoff_seconds=backoff_seconds, operation="delete_connector",
    )
