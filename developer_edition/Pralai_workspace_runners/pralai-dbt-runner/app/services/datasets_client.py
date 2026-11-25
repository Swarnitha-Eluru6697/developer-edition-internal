"""Delivery to the Datasets product (TDD 4.4.2), RUN-DBT-007/-008.

Building the payload (which models from a successful run become datasets)
stays in run_service.py. This module only does the HTTP call — used
exclusively by app.services.metadata_outbox's durable delivery worker.

Auth (RUN-DBT-007): every call carries a short-lived JWT scoped to exactly
datasets.register.write and audienced to the Datasets service, minted from
settings.datasets_internal_token — the same pattern as the UCS lineage
path. No token configured means the sink is disabled (nothing is sent, and
run finalization does not even enqueue registrations).

Idempotency (RUN-DBT-008): the outbox row's idempotency key is sent as an
`Idempotency-Key` header so retried deliveries can be de-duplicated.
"""
from __future__ import annotations

import httpx
import structlog

from app.core.service_auth import DATASETS_AUDIENCE, mint_scoped_token

log = structlog.get_logger()

_TIMEOUT = 15.0
DATASETS_SCOPE = "datasets.register.write"


async def deliver(payload: dict, settings, *, idempotency_key: str | None = None) -> dict:
    """Send an already-built {"datasets": [...]} payload. Never raises —
    a failure is reported back to the caller, which persists it and
    retries on the next sweep."""
    if not settings.datasets_url:
        return {"sent": False, "reason": "DATASETS_URL not configured"}
    if not settings.datasets_internal_token:
        return {"sent": False, "reason": "DATASETS_INTERNAL_TOKEN not configured"}

    token = mint_scoped_token(
        secret=settings.datasets_internal_token, audience=DATASETS_AUDIENCE, scope=DATASETS_SCOPE,
        subject=settings.service_name, org_id=settings.org_id,
    )
    headers = {"Authorization": f"Bearer {token}"}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key

    url = f"{settings.datasets_url.rstrip('/')}/v1/datasets/register-batch"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.post(url, json=payload, headers=headers)
    except httpx.HTTPError as e:
        log.warning("datasets_deliver_failed", error=str(e))
        return {"sent": False, "reason": f"datasets unreachable: {e}"}

    ok = 200 <= r.status_code < 300
    log.info("datasets_delivered", status=r.status_code, count=len(payload.get("datasets") or []))
    return {"sent": ok, "status": r.status_code, "reason": None if ok else r.text[:200]}
