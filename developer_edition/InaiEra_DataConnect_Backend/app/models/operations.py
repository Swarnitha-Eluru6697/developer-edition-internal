"""
Operation model — a generic, pollable resource for lifecycle actions that
touch a live external system (Kafka Connect for pipeline deploy/pause/
resume/restart, the source system itself for connection discover) and
therefore shouldn't tie the HTTP request's lifetime to how long that system
takes to respond.

This is the same shape as QualityExecutionJob (app/models/quality.py),
generalized: `operation_type` + `resource_type`/`resource_id` replace that
model's pipeline_id/quality_policy_id pair so one table covers every kind of
long-running action instead of one table per action. QualityExecutionJob
itself is left as-is — it already has this pattern and a caller-visible
result type (QualityResult) worth keeping distinct — but any *new*
long-running action should use this table, not grow a new one-off job model.

`result` is a JSONB snapshot of whatever the synchronous endpoint used to
return (e.g. a PipelineStatus or DiscoveryResult body) — there's no single
result table to point at across operation types the way QualityExecutionJob
points at QualityResult, so the response itself is stored directly.
"""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UuidPk


class Operation(Base, UuidPk):
    """
    A submitted long-running action, tracked from `queued` through a
    worker-claimed `running` to a terminal `succeeded`/`failed`/`stale`
    state — see app/services/operation_runner.py for how it's driven, and
    app/api/routes/operations.py for GET /operations/{id}.

    Durable across a process restart, unlike QualityExecutionJob/
    quality_job_runner.py (which can't be, since the batch of `records` it
    runs against is never persisted — see that model's own docstring):
    `operation_type` + `resource_type`/`resource_id` are always enough to
    reconstruct the work from scratch (redeploy this pipeline, re-discover
    this connection, ...), so a row a worker claimed is safe to hand to a
    *different* worker process — no in-memory closure to lose. The lifecycle
    is claim-based, not just a status label:

        queued
          -> a worker claims it: status=running, attempt_count += 1,
             lease_expires_at = now + lease duration
          -> the worker heartbeats lease_expires_at forward while it works
          -> succeeded/failed, OR — if the worker's process dies mid-run —
             the lease simply expires and ANY worker's next poll reclaims
             the row (status stays "running" the whole time; only
             lease_expires_at and attempt_count reveal the reclaim)

    attempt_count caps retries (see operation_runner._MAX_ATTEMPTS) so a
    row whose work reliably crashes the process every time it's attempted
    eventually gets marked failed instead of being reclaimed forever.

    For pipeline lifecycle operations specifically this durability is on
    top of an existing safety net, not the only one: the runtime reconciler
    (see app/services/runtime_reconciler.py) independently converges the
    pipeline's own desired vs. observed state regardless of whether this
    Operation row ever reaches a terminal status, so the pipeline itself
    doesn't get stuck even if its Operation record somehow did.
    """

    __tablename__ = "dc_operations"
    __table_args__ = (CheckConstraint("result_status IN ('valid', 'invalid')", name="ck_dc_operations_result_status"),)

    org_id: Mapped[str] = mapped_column(ForeignKey("dc_organizations.org_id"), index=True)
    operation_type: Mapped[str] = mapped_column(
        String(100), index=True,
        comment="pipeline_deploy | pipeline_pause | pipeline_resume | pipeline_restart | connection_discover",
    )
    resource_type: Mapped[str] = mapped_column(String(50), comment="pipeline | discovery_run")
    resource_id: Mapped[str] = mapped_column(String(255), index=True)
    status: Mapped[str] = mapped_column(
        String(50), default="queued",
        # FE-BUG-05: "stale" is distinct from "failed" — attempt_count
        # exhausted (see operation_runner._execute) without the work itself
        # ever being observed to raise, vs. a real error from the work
        # itself. A caller (eventually a browser polling this) should never
        # have to guess which one "failed" actually meant.
        comment="queued | running | succeeded | failed | stale",
    )
    # GAP-04: `status` is the EXECUTION outcome (did the action run); this is the RESULT-CONTRACT outcome (did what it returned
    # satisfy its contract and size limit, so it could be stored). They are independent: a connector can be deployed
    # (status=succeeded) while its result is malformed (result_status=invalid, error_code=OPERATION_RESULT_INVALID, result NULL).
    # NULL while the operation is not finished, or finished without a result to check (failed / stale).
    result_status: Mapped[str | None] = mapped_column(String(20), comment="valid | invalid")
    result: Mapped[dict | None] = mapped_column(JSONB)
    error_message: Mapped[str | None] = mapped_column(Text)
    # BUG-11: error_message above is now always a SafeError's generic,
    # non-leaking message (app/services/safe_error.py) — these carry the
    # rest of that same classification. error_code/retryable are null for
    # anything that never failed — except an action that succeeded with an invalid RESULT (GAP-04), which carries
    # OPERATION_RESULT_INVALID here while status stays "succeeded"; correlation_id ties error_message back to
    # the (credential-scrubbed but otherwise detailed) diagnostic log entry
    # an operator would actually need to debug the real cause.
    error_code: Mapped[str | None] = mapped_column(String(100))
    retryable: Mapped[bool | None] = mapped_column()
    correlation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # NULL means "never claimed" (still queued) — a claimed row always has
    # this set, and a value in the past (including a pre-existing NULL on a
    # row claimed by the old, pre-lease code) marks it reclaimable.
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    attempt_count: Mapped[int] = mapped_column(default=0, server_default="0")
