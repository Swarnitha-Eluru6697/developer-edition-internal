"""Transactional outbox -> UCS, at-least-once (DQ-013, QualityCheckRegistration).

enqueue() is called INSIDE the same DB transaction as the change it
describes (rule/ruleset/binding change, apply), so the event exists iff the
change committed. Duplicate enqueues collapse on idempotency_key.

drain_once() claims due `pending` rows with FOR UPDATE SKIP LOCKED (safe
with several replicas), delivers each with its idempotency key as the
Idempotency-Key header, and:
  2xx / 409 (already have it)  -> delivered
  other 4xx (except 408/429)   -> failed (permanent; kept for inspection)
  5xx / unreachable / contract
  not enabled yet              -> stays pending, exponential backoff
Nothing is ever dropped.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.integrations.common import UpstreamError
from app.integrations.ucs import UcsClient, UcsContractUnavailable
from app.models import OutboxEvent

log = structlog.get_logger()

KIND_AUDIT = "audit_event"
KIND_CHECK_REGISTRATION = "check_registration"


async def enqueue(db: AsyncSession, *, org_id: str, kind: str, idempotency_key: str,
                  payload: dict[str, Any]) -> None:
    await db.execute(
        pg_insert(OutboxEvent)
        .values(org_id=org_id, kind=kind, idempotency_key=idempotency_key[:255], payload=payload)
        .on_conflict_do_nothing(index_elements=["idempotency_key"]))


async def _deliver(client: UcsClient, row: OutboxEvent):
    if row.kind == KIND_AUDIT:
        return await client.post_audit_event(row.payload, org_id=row.org_id,
                                             idempotency_key=row.idempotency_key)
    if row.kind == KIND_CHECK_REGISTRATION:
        return await client.register_check(row.payload, org_id=row.org_id,
                                           idempotency_key=row.idempotency_key)
    raise ValueError(f"unknown outbox kind {row.kind!r}")


async def drain_once(session_factory=None, *, limit: int | None = None,
                     client: UcsClient | None = None) -> dict[str, int]:
    from app.db import session as db_session

    settings = get_settings()
    factory = session_factory or db_session.AsyncSessionLocal
    client = client or UcsClient(settings)
    stats = {"delivered": 0, "failed": 0, "retry": 0}
    now = datetime.now(timezone.utc)
    async with factory() as db:
        rows = (await db.execute(
            select(OutboxEvent)
            .where(OutboxEvent.status == "pending", OutboxEvent.next_attempt_at <= now)
            .order_by(OutboxEvent.created_at)
            .limit(limit or settings.outbox_batch_size)
            .with_for_update(skip_locked=True))).scalars().all()
        for row in rows:
            error: str | None = None
            permanent = False
            try:
                r = await _deliver(client, row)
                if 200 <= r.status_code < 300 or r.status_code == 409:
                    row.status = "delivered"
                    row.delivered_at = datetime.now(timezone.utc)
                    row.last_error = None
                    stats["delivered"] += 1
                    continue
                error = f"HTTP {r.status_code}: {r.text[:300]}"
                permanent = 400 <= r.status_code < 500 and r.status_code not in (408, 425, 429)
            except UcsContractUnavailable as e:
                error = f"not delivered yet: {e}"
            except (UpstreamError, ValueError) as e:
                error = str(e)
            row.attempts += 1
            row.last_error = error
            if permanent:
                row.status = "failed"
                stats["failed"] += 1
            else:
                delay = min(settings.outbox_max_backoff_seconds,
                            settings.outbox_poll_interval_seconds * (2 ** min(row.attempts, 16)))
                row.next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
                stats["retry"] += 1
        await db.commit()
    if any(stats.values()):
        log.info("dq_outbox.drained", **stats)
    return stats


async def outbox_worker() -> None:
    settings = get_settings()
    while True:
        try:
            await drain_once()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — the loop must survive any single failure
            log.warning("dq_outbox.drain_failed", error=str(e))
        await asyncio.sleep(settings.outbox_poll_interval_seconds)
