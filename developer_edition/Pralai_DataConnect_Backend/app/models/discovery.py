"""
Discovery models (GAP-10).

Connection discovery (POST /connections/{id}/discover, still executed via
the same durable Operation claim/lease worker as every other long-running
action — see app/services/operation_runner.py) used to dump its ENTIRE
result — every discovered schema, table, and (eventually) column — into
Operation.result, a single generic JSONB column. A real source can return
tens of thousands of objects; that's not a viable shape for one JSON blob,
and it gives the API no way to search or page through a large result
without loading all of it first.

DiscoveryRun is the small, pollable summary (status/object_count/
continuation) that now lives where Operation.result used to — Operation
itself still exists underneath and still drives claim/lease/retry/attempt
tracking exactly as before, it's just no longer where the actual objects
live. DiscoveryObject is one row per discovered schema/table, queried
through GET /discovery/{id}/objects (app/api/routes/discovery.py) with
search + cursor pagination instead of being read back as one blob.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UuidPk


class DiscoveryRun(Base, UuidPk, TimestampMixin):
    """One row per POST /connections/{id}/discover call — the resource
    `GET /discovery/{id}` and `GET /discovery/{id}/objects` address.

    `operation_id` links back to the underlying Operation (resource_type=
    "discovery_run", resource_id=this row's id — see
    app/api/routes/connections.py's _discover_factory) that actually claims
    and executes the work; this row is what the API surfaces to callers
    instead. `continuation` is reserved for a future incremental/resumable
    discovery pass (e.g. a source whose own list-objects API is itself
    paginated) — every discover() call today runs to completion in one
    pass, so it's always null for now; kept here so the shape doesn't need
    to change again once that's built.
    """

    __tablename__ = "dc_discovery_runs"

    org_id: Mapped[str] = mapped_column(ForeignKey("dc_organizations.org_id"), index=True)
    connection_id: Mapped[str] = mapped_column(String(64), index=True)
    operation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), default="queued", comment="queued | running | succeeded | failed",
    )
    object_count: Mapped[int] = mapped_column(default=0, server_default="0")
    continuation: Mapped[str | None] = mapped_column(String(500), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    truncated: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false",
        comment="The source exposed more objects than settings.discovery_max_objects; only the first ones are stored.",
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DiscoveryObject(Base, UuidPk):
    """One discovered schema or table, belonging to exactly one
    DiscoveryRun. No TimestampMixin/updated_at — these rows are written
    once, in bulk, at the end of a discovery run and never edited; a
    re-discover creates a brand new DiscoveryRun (and its own objects)
    rather than mutating a prior one, the same way a redeployed pipeline
    gets a new generation rather than editing history in place.

    Listed in (schema, name, id) order with a keyset cursor over that key
    (app/repositories/discovery_cursor.py), so pages are stable and readable."""

    __tablename__ = "dc_discovery_objects"
    discovery_run_id: Mapped[str] = mapped_column(String(64), index=True)
    org_id: Mapped[str] = mapped_column(String(100), index=True)
    object_type: Mapped[str] = mapped_column(String(20), comment="schema | table")
    schema_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    name: Mapped[str] = mapped_column(String(500))

    __table_args__ = (
        # The key GET /discovery/{id}/objects pages by (app/repositories/discovery_cursor.py): schema, name, id.
        Index("ix_dc_discovery_objects_run_order", "discovery_run_id", func.coalesce(schema_name, ""), "name", "id"),
    )
