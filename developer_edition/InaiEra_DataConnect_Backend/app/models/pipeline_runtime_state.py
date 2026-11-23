"""Durable runtime-reconciliation state for a pipeline's Kafka Connect connector.

One row per pipeline (created the first time it's deployed/paused/resumed/
restarted — see runtime_reconciler.record_desired — so a never-deployed
pipeline has none yet). This is the reconciler's source of truth for what
*should* be running (desired_state/desired_generation/config_hash) versus
what Kafka Connect last reported (observed_state/observed_generation),
independent of dc_pipelines' own `status` column, which only reflects the
mapped outcome as of the last successful reconcile tick (see
OBSERVED_TO_PIPELINE_STATUS in app/services/runtime_reconciler.py).

FE-BUG-07: last_reconciled_at updates every tick this row is claimed,
whether or not that tick actually got a real answer from Kafka Connect
(see runtime_reconciler._reconcile_row) — so it means "last time we tried",
not "last time we knew the truth". last_observed_at is the distinct field
for that: it only advances when observed_state was actually derived from a
genuine Connect response (or a genuine "not there" from the connector
list), never on a tick that merely attempted and failed. The frontend's
"Last synced" (converged) vs "Last observed" (observed_state=unknown, e.g.
Connect unreachable) both read this field, never last_reconciled_at — the
whole point is not to let a merely-attempted tick masquerade as a real,
fresh observation.

leased_by/lease_expires_at (GAP-08): which RuntimeReconciler process
currently owns reconciling this row, and until when. More than one process
can run `runtime_reconciler.run_forever()` at once (e.g. two API server
replicas) with no other coordination between them — see
app/services/runtime_reconciler.py's module docstring for the atomic claim
query that uses these columns to make sure only one process actually acts
on a given pipeline at a time, and how ownership transfers to another live
process once a dead one's lease lapses.
"""

from datetime import datetime

from sqlalchemy import DateTime, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UuidPk


class PipelineRuntimeState(Base, UuidPk, TimestampMixin):
    __tablename__ = "dc_pipeline_runtime_state"
    __table_args__ = (UniqueConstraint("pipeline_id"),)

    # No FK to dc_pipelines/dc_organizations: pipelines live in the in-memory
    # store today, not Postgres, so their ids aren't guaranteed to exist there.
    org_id: Mapped[str] = mapped_column(String(100), index=True)
    pipeline_id: Mapped[str] = mapped_column(String(64), index=True)
    connector_name: Mapped[str] = mapped_column(String(500))

    # The worker pool (Kafka Connect cluster) this connector lives in — copied from the pipeline when its desired state is first
    # recorded. The reconciler needs it to reach the right cluster even after the pipeline row is gone (orphan clean-up).
    runtime_pool_id: Mapped[str | None] = mapped_column(String(255))

    desired_state: Mapped[str] = mapped_column(String(50))
    desired_generation: Mapped[int] = mapped_column(default=1)
    config_hash: Mapped[str | None] = mapped_column(String(64))

    observed_state: Mapped[str] = mapped_column(String(50), default="unknown")
    observed_generation: Mapped[int] = mapped_column(default=0)

    last_reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # FE-BUG-07: distinct from last_reconciled_at — see this model's own
    # docstring above.
    last_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_runtime_error: Mapped[str | None] = mapped_column(Text)

    # GAP-03: the quality stream worker for this pipeline is stuck on a record it can neither handle nor dead-letter, so it will not
    # commit past it. `since` is when the current record first failed; `heartbeat_at` is refreshed while it stays stuck (a stale
    # one means the worker is gone). Both are NULL when nothing is stuck. See app/services/quality_worker_health.py.
    quality_blocked_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    quality_blocked_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    leased_by: Mapped[str | None] = mapped_column(String(200), index=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
