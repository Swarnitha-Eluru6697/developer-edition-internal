"""Restart-safe lifecycle, leases, generations, preview audit
(RUN-FLINK-013/014/015/016).

ADDITIVE ONLY — safe to apply to a database already at 0005 holding live
jobs:
  * every new column is NULLable or has a server default;
  * the 'submitting' de-duplication runs again before (re)asserting the
    one-active-generation unique index with IF NOT EXISTS, so a database
    that already applied 0005 is unaffected and one that somehow lacks the
    index gets it without failing;
  * existing deployments get generation numbers backfilled per job in
    creation order, and flink_jobs.generation_counter is set to match;
  * metric columns that can exceed int4 (heap bytes, checkpoint size, lag,
    watermark lag) are widened to BIGINT (a widening cast — no data change).

Revision ID: 0006_lifecycle_leases_audit
Revises:     0005_deploy_stop_idempotency
"""
from __future__ import annotations

import importlib.util
import os
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_lifecycle_leases_audit"
down_revision: Union[str, None] = "0005_deploy_stop_idempotency"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _dedupe_sql() -> str:
    path = os.path.join(os.path.dirname(__file__), "0005_deploy_stop_idempotency.py")
    spec = importlib.util.spec_from_file_location("_m0005", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod.DEDUPE_SUBMITTING_SQL


_WIDEN = ("kafka_consumer_lag", "last_checkpoint_size_bytes", "heap_used_bytes",
          "heap_max_bytes", "watermark_lag_ms")


def upgrade() -> None:
    # ── RUN-FLINK-014: de-duplicate, then (re)assert the unique index ──────
    op.execute(_dedupe_sql())
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_flink_dep_active_generation "
        "ON flink_deployments (job_id) WHERE status = 'submitting'"
    )

    # ── flink_jobs ───────────────────────────────────────────────────────
    op.add_column("flink_jobs", sa.Column("generation_counter", sa.Integer, nullable=False,
                                          server_default="0"))
    op.add_column("flink_jobs", sa.Column("savepoint_generation", sa.Integer))
    op.add_column("flink_jobs", sa.Column("savepoint_sql_hash", sa.String(64)))

    # ── flink_deployments ────────────────────────────────────────────────
    for name, type_ in (
        ("idempotency_request_hash", sa.String(64)),
        ("stop_idempotency_request_hash", sa.String(64)),
        ("generation", sa.Integer),
        ("sql_hash", sa.String(64)),
        ("submission_tag", sa.String(255)),
        ("gateway_operation_handle", sa.String(255)),
        ("submit_started_at", sa.DateTime(timezone=True)),
        ("stop_trigger_id", sa.String(255)),
        ("owner_id", sa.String(255)),
        ("lease_expires_at", sa.DateTime(timezone=True)),
        ("heartbeat_at", sa.DateTime(timezone=True)),
    ):
        op.add_column("flink_deployments", sa.Column(name, type_))
    op.add_column("flink_deployments", sa.Column(
        "consumer_groups", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")))

    # Backfill generations for existing rows (creation order per job).
    op.execute("""
        UPDATE flink_deployments d
           SET generation = ranked.rn
          FROM (SELECT id, row_number() OVER (PARTITION BY job_id
                                              ORDER BY created_at, id) AS rn
                  FROM flink_deployments) ranked
         WHERE d.id = ranked.id
    """)
    op.execute("""
        UPDATE flink_jobs j
           SET generation_counter = g.max_gen
          FROM (SELECT job_id, max(generation) AS max_gen
                  FROM flink_deployments GROUP BY job_id) g
         WHERE j.id = g.job_id
    """)
    op.create_index("uq_flink_dep_job_generation", "flink_deployments", ["job_id", "generation"],
                    unique=True, postgresql_where=sa.text("generation IS NOT NULL"))
    op.create_index("ix_flink_dep_lease", "flink_deployments", ["status", "lease_expires_at"])

    # ── flink_metric_snapshots: widen int4 -> int8 ───────────────────────
    for col in _WIDEN:
        op.alter_column("flink_metric_snapshots", col, type_=sa.BigInteger,
                        existing_type=sa.Integer, existing_nullable=True)

    # ── flink_savepoints ─────────────────────────────────────────────────
    op.create_table(
        "flink_savepoints",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("job_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("flink_jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("deployment_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("flink_deployments.id", ondelete="SET NULL")),
        sa.Column("generation", sa.Integer),
        sa.Column("sql_hash", sa.String(64)),
        sa.Column("path", sa.String(1000), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False, server_default="savepoint"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("kind IN ('savepoint', 'checkpoint')", name="ck_flink_savepoint_kind"),
        sa.UniqueConstraint("job_id", "path", name="uq_flink_savepoint_job_path"),
    )
    op.create_index("ix_flink_savepoints_job", "flink_savepoints", ["job_id", "created_at"])

    # ── kafka_preview_audit ──────────────────────────────────────────────
    op.create_table(
        "kafka_preview_audit",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("org_id", sa.String(255), nullable=False),
        sa.Column("user_id", sa.String(255), nullable=False),
        sa.Column("action", sa.String(20), nullable=False),
        sa.Column("topic", sa.String(500)),
        sa.Column("allowed", sa.Boolean, nullable=False),
        sa.Column("reason", sa.Text),
        sa.Column("message_count", sa.Integer),
        sa.Column("bytes_returned", sa.Integer),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("action IN ('list_topics', 'schema', 'preview')",
                           name="ck_kafka_preview_audit_action"),
    )
    op.create_index("ix_kafka_preview_audit_org_ts", "kafka_preview_audit", ["org_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_kafka_preview_audit_org_ts", table_name="kafka_preview_audit")
    op.drop_table("kafka_preview_audit")
    op.drop_index("ix_flink_savepoints_job", table_name="flink_savepoints")
    op.drop_table("flink_savepoints")
    for col in _WIDEN:
        op.alter_column("flink_metric_snapshots", col, type_=sa.Integer,
                        existing_type=sa.BigInteger, existing_nullable=True)
    op.drop_index("ix_flink_dep_lease", table_name="flink_deployments")
    op.drop_index("uq_flink_dep_job_generation", table_name="flink_deployments")
    for name in ("consumer_groups", "heartbeat_at", "lease_expires_at", "owner_id",
                 "stop_trigger_id", "submit_started_at", "gateway_operation_handle",
                 "submission_tag", "sql_hash", "generation",
                 "stop_idempotency_request_hash", "idempotency_request_hash"):
        op.drop_column("flink_deployments", name)
    op.drop_column("flink_jobs", "savepoint_sql_hash")
    op.drop_column("flink_jobs", "savepoint_generation")
    op.drop_column("flink_jobs", "generation_counter")
