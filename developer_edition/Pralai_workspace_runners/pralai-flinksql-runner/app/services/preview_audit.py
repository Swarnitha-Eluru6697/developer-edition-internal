"""Durable audit trail for Kafka catalog access (RUN-FLINK-016).

Every topic listing, schema lookup and raw-message preview — allowed or
denied — is written to kafka_preview_audit BEFORE any data is returned. A
preview whose audit row cannot be written is refused outside dev (an
un-audited raw-data read is exactly what governance must not allow); in
dev the failure is logged and the request proceeds.
"""
from __future__ import annotations

import uuid

import structlog
from fastapi import HTTPException, status
from sqlalchemy import update

from app.core.config import get_settings
from app.db.session import AsyncSessionLocal
from app.models import KafkaPreviewAudit

log = structlog.get_logger()


async def record_access(
    *, org_id: str, user_id: str, action: str, topic: str | None, allowed: bool,
    reason: str | None = None, message_count: int | None = None,
) -> uuid.UUID | None:
    audit_id = uuid.uuid4()
    log.info("kafka_catalog_access", org_id=org_id, user_id=user_id, action=action,
             topic=topic, allowed=allowed, reason=reason)
    try:
        async with AsyncSessionLocal() as db:
            db.add(KafkaPreviewAudit(
                id=audit_id, org_id=org_id, user_id=user_id, action=action, topic=topic,
                allowed=allowed, reason=(reason or None) and reason[:2000],
                message_count=message_count,
            ))
            await db.commit()
        return audit_id
    except Exception as e:  # noqa: BLE001
        log.error("kafka_catalog_audit_write_failed", error=str(e), action=action, topic=topic)
        if get_settings().environment != "dev":
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"code": "AUDIT_UNAVAILABLE",
                        "message": "Kafka catalog access cannot be audited right now"},
            )
        return None


async def record_result(audit_id: uuid.UUID | None, *, message_count: int, bytes_returned: int) -> None:
    if audit_id is None:
        return
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(update(KafkaPreviewAudit).where(KafkaPreviewAudit.id == audit_id)
                             .values(message_count=message_count, bytes_returned=bytes_returned))
            await db.commit()
    except Exception as e:  # noqa: BLE001
        log.error("kafka_catalog_audit_update_failed", error=str(e), audit_id=str(audit_id))
