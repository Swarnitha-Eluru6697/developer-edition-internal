"""Audit Log model — immutable record of every mutating API action."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UuidPk


class AuditLog(Base, UuidPk):
    """
    Append-only audit trail for all create / update / delete operations.

    Written by route handlers (or a middleware) whenever a resource is mutated.
    No updated_at column — audit rows are never modified after insertion.

    Fields:
        actor           — user or service account that performed the action,
                          supplied by the platform/auth layer (see
                          app/core/actor.py) — this model neither derives nor
                          validates identity itself
        action          — verb: created | updated | deleted | deployed | paused ...
        resource_type   — connection | pipeline | transform_policy | schema_version ...
        resource_id     — UUID or identifier of the affected resource
        before_state    — snapshot of the resource before the change (None for creates)
        after_state     — snapshot of the resource after the change (None for deletes)

    org_id is nullable: most audit records are org-scoped, but a handful of
    resources are platform-global rather than per-org (connector templates —
    see app/services/catalog_service.py — have no org context at all), and
    those are recorded with org_id=None rather than a fabricated org. See
    app/core/audit.py's record_audit(), the one place every mutating
    operation should go through to write a row here.
    """

    __tablename__ = "dc_audit_logs"

    org_id: Mapped[str | None] = mapped_column(ForeignKey("dc_organizations.org_id"), index=True)
    actor: Mapped[str | None] = mapped_column(String(255))
    action: Mapped[str] = mapped_column(String(255))
    resource_type: Mapped[str | None] = mapped_column(String(100))
    resource_id: Mapped[str | None] = mapped_column(String(255))
    before_state: Mapped[dict | None] = mapped_column(JSONB)
    after_state: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
