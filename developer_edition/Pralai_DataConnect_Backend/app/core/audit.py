"""
Generic audit-record writer — every mutating operation across DataConnect
should call record_audit() so all durable audit trails share one shape, one
session-handling pattern, and one place to extend, rather than each service
hand-rolling its own AuditLogRepository call inline (which is what the one
pre-existing call site, provision_topic_and_record_audit in
app/services/kafka_admin.py, had to do before this module existed — that
call site is unchanged, but new ones should use record_audit() instead of
copying its pattern).

Identity/authentication is out of scope for this module too — see
app/core/actor.py. record_audit() accepts whatever actor string the caller
(ultimately, the platform/auth layer, via the get_actor dependency) supplies;
it never derives, verifies, or defaults identity itself beyond what's passed
in.

Audit writes are Postgres-only (AuditLogRepository), regardless of whether
the mutation itself is memory-store-backed (most services in this codebase
still are — connections, pipelines, transform policies, quality policies)
or already Postgres-backed (connector templates, schema drift approvals).
Each call opens its own short-lived session/transaction rather than trying
to share one with the underlying mutation, mirroring the only pattern this
codebase had established before this module existed
(provision_topic_and_record_audit). This means an audit write for a
memory-store mutation can never be truly atomic with it — if the audit
write fails after the mutation already happened, the mutation is not rolled
back (the in-memory store has no transactions to roll back). A failed audit
write is deliberately NOT swallowed here, though: it propagates like any
other unexpected error, so the caller (and the client) sees the request as
failed and knows to retry, rather than an audit trail silently going missing
while the API reports success.
"""

from typing import Any

from fastapi.encoders import jsonable_encoder

from app.db.session import AsyncSessionLocal
from app.repositories.dataconnect import AuditLogRepository


async def record_audit(
    *,
    org_id: str | None,
    actor: str,
    action: str,
    resource_type: str,
    resource_id: str | None,
    before_state: dict[str, Any] | None = None,
    after_state: dict[str, Any] | None = None,
) -> None:
    """Write one immutable AuditLog row. `org_id=None` is only for the rare
    platform-global resource (e.g. a connector template) that has no org
    context at all — every org-scoped mutation must pass its real org_id.

    before_state/after_state are run through FastAPI's own jsonable_encoder
    before being handed to the JSONB column: callers pass the same plain
    service-layer dict a route returns to its client (e.g.
    mask_connection_profile's output, which includes last_tested_at as a
    real datetime — Pydantic/FastAPI knows how to encode that for an HTTP
    response, but the raw dict going straight into a JSONB column via
    asyncpg does not, and previously crashed with "Object of type datetime
    is not JSON serializable" on every connection creation, leaving the
    resource created but the request itself failing). Converting here,
    once, is the fix every caller would otherwise have to remember to do
    itself (and every future caller could just as easily forget)."""
    async with AsyncSessionLocal() as session:
        await AuditLogRepository(session).create(
            {
                "org_id": org_id,
                "actor": actor,
                "action": action,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "before_state": jsonable_encoder(before_state) if before_state is not None else None,
                "after_state": jsonable_encoder(after_state) if after_state is not None else None,
            }
        )
        await session.commit()
