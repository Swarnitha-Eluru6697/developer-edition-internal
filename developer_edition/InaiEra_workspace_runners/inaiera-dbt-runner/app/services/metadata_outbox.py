"""Durable metadata outbox (RUN-DBT-008).

Run completion used to publish lineage (to UCS) and dataset registration
(to the Datasets product) as best-effort, inline HTTP calls: if either
service was briefly unavailable, or the process crashed right after the
dbt build succeeded but before the call went out, that metadata was
simply gone — nothing remembered it needed to be sent.

enqueue() writes a durable row in the exact same database transaction
that marks a Run terminal (see run_service._finalize_run()), so the two
either both happen or neither does — there is no window where a run is
recorded as succeeded but the event announcing it never gets persisted.
A separate background worker (outbox_loop(), wired into the app's
lifespan next to the run reconciler and scheduler) then delivers pending
rows and retries with exponential backoff until the endpoint acknowledges
(2xx). Retries are bounded: after metadata_outbox_max_attempts failures a
row moves to 'dead_letter' (logged at ERROR, counted in app.core.metrics,
surfaced by /readyz) for operator attention instead of retrying forever.
A row whose sink has since been disabled is marked 'skipped' rather than
left pending forever. Every delivery carries the row's idempotency_key as
an `Idempotency-Key` header so the receiver can de-duplicate retries.

Claiming a row (_claim()) uses the same guarded-UPDATE pattern as the run
reconciler and scheduler: a lease is written only if the row is still
'pending' and due, so two replicas racing to deliver the same row is
naturally rare, and even if it did happen the actual send methods
(openlineage_emitter.deliver(), datasets_client.deliver()) are read-then-
POST calls whose worst case is a duplicate delivery attempt — which the
receiver collapses using the Idempotency-Key header — not a duplicate Run
or a duplicate DB row.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import metrics
from app.core.config import get_settings
from app.db.session import AsyncSessionLocal
from app.executors import openlineage_emitter
from app.models import MetadataOutboxEvent
from app.services import datasets_client

log = structlog.get_logger()

# How long a claimed row is "leased" before another sweep would consider
# it stale and retry it — comfortably longer than one delivery attempt's
# timeout (15-30s) so a slow-but-successful attempt is never double-sent.
_CLAIM_LEASE_SECONDS = 120


async def enqueue(
    db: AsyncSession, *, run_id: uuid.UUID, project_id: uuid.UUID,
    event_type: str, payload: dict,
) -> None:
    """Persist one metadata event for later delivery. Must be called on a
    session that is still inside the same transaction as the run's
    terminal-state update (i.e. before that transaction's commit) — that
    is what makes the two atomic. A no-op if this exact (event_type,
    run_id) was already enqueued (checked here rather than relying solely
    on the table's unique constraint, since a caught IntegrityError would
    otherwise abort the whole surrounding transaction)."""
    idempotency_key = f"{event_type}:{run_id}"
    existing = await db.execute(
        select(MetadataOutboxEvent.id).where(MetadataOutboxEvent.idempotency_key == idempotency_key)
    )
    if existing.scalar_one_or_none() is not None:
        return
    db.add(MetadataOutboxEvent(
        run_id=run_id, project_id=project_id, event_type=event_type,
        idempotency_key=idempotency_key, payload=payload,
    ))


def _next_backoff(attempt_count: int, settings) -> datetime:
    delay = min(
        settings.metadata_outbox_initial_backoff_seconds * (2 ** attempt_count),
        settings.metadata_outbox_max_backoff_seconds,
    )
    return datetime.now(timezone.utc) + timedelta(seconds=delay)


async def _claim(event_id: uuid.UUID) -> bool:
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            update(MetadataOutboxEvent)
            .where(MetadataOutboxEvent.id == event_id, MetadataOutboxEvent.status == "pending",
                   MetadataOutboxEvent.next_attempt_at <= now)
            .values(next_attempt_at=now + timedelta(seconds=_CLAIM_LEASE_SECONDS))
        )
        await db.commit()
        return result.rowcount == 1


def _sink_enabled(event_type: str, settings) -> bool:
    if event_type == "openlineage":
        return settings.openlineage_sink_enabled
    if event_type == "dataset_registration":
        return settings.datasets_sink_enabled
    return False


async def _deliver_event(event_type: str, payload: dict, settings, *, idempotency_key: str) -> dict:
    if event_type == "openlineage":
        # openlineage_emitter.deliver() is a plain blocking function — run
        # it off the event loop so a slow UCS never stalls other deliveries.
        return await asyncio.to_thread(openlineage_emitter.deliver, payload, settings,
                                       idempotency_key=idempotency_key)
    if event_type == "dataset_registration":
        return await datasets_client.deliver(payload, settings, idempotency_key=idempotency_key)
    return {"sent": False, "reason": f"unknown event_type: {event_type}"}


async def _finalize_delivery(event_id: uuid.UUID, *, ok: bool, reason: str | None, settings,
                             skipped: bool = False) -> str | None:
    """Record the outcome of one attempt; returns the row's new status."""
    async with AsyncSessionLocal() as db:
        event = await db.get(MetadataOutboxEvent, event_id)
        if not event:
            return None
        if skipped:
            event.status = "skipped"
            event.last_error = (reason or "")[:1000]
            metrics.inc("metadata_outbox_skipped_total", event_type=event.event_type)
        elif ok:
            event.status = "delivered"
            event.delivered_at = datetime.now(timezone.utc)
            event.last_error = None
            metrics.inc("metadata_outbox_delivered_total", event_type=event.event_type)
        else:
            event.attempt_count += 1
            event.last_error = (reason or "")[:1000]
            metrics.inc("metadata_outbox_failed_attempts_total", event_type=event.event_type)
            if event.attempt_count >= settings.metadata_outbox_max_attempts:
                event.status = "dead_letter"
                metrics.inc("metadata_outbox_dead_letter_total", event_type=event.event_type)
                log.error("metadata_outbox_dead_lettered", event_id=str(event_id),
                          event_type=event.event_type, run_id=str(event.run_id),
                          attempts=event.attempt_count, last_error=event.last_error)
            else:
                event.next_attempt_at = _next_backoff(event.attempt_count, settings)
        status = event.status
        await db.commit()
        return status


async def deliver_pending(*, batch_size: int = 20) -> None:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(MetadataOutboxEvent.id)
            .where(MetadataOutboxEvent.status == "pending", MetadataOutboxEvent.next_attempt_at <= now)
            .order_by(MetadataOutboxEvent.created_at)
            .limit(batch_size)
        )
        event_ids = [row[0] for row in result]

    for event_id in event_ids:
        if not await _claim(event_id):
            continue  # another replica's sweep already picked this row up

        async with AsyncSessionLocal() as db:
            event = await db.get(MetadataOutboxEvent, event_id)
        if not event:
            continue

        if not _sink_enabled(event.event_type, settings):
            log.warning("metadata_outbox_sink_disabled_skipping", event_id=str(event_id),
                        event_type=event.event_type)
            await _finalize_delivery(event_id, ok=False, settings=settings, skipped=True,
                                     reason=f"sink for {event.event_type} is not configured")
            continue

        outcome = await _deliver_event(event.event_type, event.payload, settings,
                                       idempotency_key=event.idempotency_key)
        ok = bool(outcome.get("sent"))
        if not ok:
            log.warning("metadata_outbox_delivery_failed", event_id=str(event_id),
                        event_type=event.event_type, reason=outcome.get("reason"))
        await _finalize_delivery(event_id, ok=ok, reason=outcome.get("reason"), settings=settings)


async def outbox_loop() -> None:
    settings = get_settings()
    log.info("metadata_outbox_starting", poll_interval=settings.metadata_outbox_poll_interval_seconds)
    while True:
        try:
            await deliver_pending()
        except Exception:
            log.exception("metadata_outbox_tick_failed")
        await asyncio.sleep(settings.metadata_outbox_poll_interval_seconds)
