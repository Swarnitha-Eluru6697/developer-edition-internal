"""Governance audit events (DQ-013).

Every rule / ruleset / ruleset-item / binding change and every apply is
recorded as a GovernanceAuditEvent in the transactional outbox, in the same
transaction as the change, and delivered to UCS at-least-once
(app.services.outbox). Each event is traceable to EXACT versions: the
resource's own monotonically increasing version, and for applies the
ArtifactVersion id + content hash + ruleset/rule versions it was built from.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.outbox import KIND_AUDIT, enqueue

EVENT_CONTRACT = "inaiera.dq.governance-audit"
EVENT_CONTRACT_VERSION = "1"


async def record(
    db: AsyncSession, *, org_id: str, actor: str, resource_type: str, resource_id: Any,
    operation: str, resource_version: Any, object_name: str | None = None,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "contract": EVENT_CONTRACT, "contract_version": EVENT_CONTRACT_VERSION,
        "event_id": str(uuid.uuid4()),
        "event_type": f"dq.{resource_type}.{operation}",
        "source": "inaiera-data-quality",
        "org_id": org_id, "actor": actor,
        "resource_type": resource_type, "resource_id": str(resource_id),
        "resource_version": str(resource_version), "operation": operation,
        "object_name": object_name,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "detail": detail or {},
    }
    # One event per change; the key is stored with the row, so every
    # delivery retry of THIS event carries the same Idempotency-Key.
    key = f"audit:{payload['event_id']}"
    await enqueue(db, org_id=org_id, kind=KIND_AUDIT, idempotency_key=key, payload=payload)
    return payload
