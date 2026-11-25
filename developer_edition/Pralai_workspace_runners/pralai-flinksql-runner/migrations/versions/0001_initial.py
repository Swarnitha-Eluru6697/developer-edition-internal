"""Initial schema for FlinkSQL Runner.
Revision ID: 0001_initial
"""
from __future__ import annotations
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001_initial"
down_revision = None

def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.create_table("flink_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("org_id", sa.String(255), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True)),
        sa.Column("created_by_user_id", sa.String(255)),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("sql", sa.Text, nullable=False),
        sa.Column("parallelism", sa.Integer, nullable=False, server_default="1"),
        sa.Column("consumer_group_id", sa.String(255)),
        sa.Column("sink_engine", sa.String(20), nullable=False, server_default="trino"),
        sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
        sa.Column("validation_errors", postgresql.ARRAY(sa.Text), nullable=False, server_default="{}"),
        sa.Column("last_deployed_at", sa.DateTime(timezone=True)),
        sa.Column("last_stopped_at", sa.DateTime(timezone=True)),
        sa.Column("savepoint_path", sa.String(1000)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('draft','validating','validated','deploying','running',"
                           "'pausing','paused','cancelling','cancelled','failed','completed')", name="ck_flink_job_status"),
        sa.UniqueConstraint("org_id", "name", name="uq_flink_job_org_name"),
    )
    op.create_index("ix_flink_jobs_org", "flink_jobs", ["org_id"])
    op.create_index("ix_flink_jobs_workspace", "flink_jobs", ["workspace_id"])

    op.create_table("flink_deployments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("flink_jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("flink_job_id", sa.String(64)),
        sa.Column("gateway_session_id", sa.String(255)),
        sa.Column("status", sa.String(20), nullable=False, server_default="submitting"),
        sa.Column("trigger", sa.String(20), nullable=False, server_default="manual"),
        sa.Column("from_savepoint", sa.String(1000)),
        sa.Column("deployed_by_user_id", sa.String(255)),
        sa.Column("error_message", sa.Text),
        sa.Column("submitted_at", sa.DateTime(timezone=True)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("stopped_at", sa.DateTime(timezone=True)),
        sa.Column("runtime_ms", sa.Integer),
        sa.Column("last_polled_at", sa.DateTime(timezone=True)),
        sa.Column("flink_state", sa.String(50)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('submitting','running','paused','cancelled','failed','completed')", name="ck_flink_dep_status"),
    )
    op.create_index("ix_flink_dep_job", "flink_deployments", ["job_id"])
    op.create_index("ix_flink_dep_flink_id", "flink_deployments", ["flink_job_id"])

    op.create_table("flink_metric_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("flink_jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("deployment_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("flink_deployments.id", ondelete="SET NULL")),
        sa.Column("captured_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("records_in_per_sec", sa.Float),
        sa.Column("records_out_per_sec", sa.Float),
        sa.Column("bytes_in_per_sec", sa.Float),
        sa.Column("bytes_out_per_sec", sa.Float),
        sa.Column("kafka_consumer_lag", sa.Integer),
        sa.Column("kafka_lag_detail", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("last_checkpoint_id", sa.Integer),
        sa.Column("last_checkpoint_size_bytes", sa.Integer),
        sa.Column("last_checkpoint_duration_ms", sa.Integer),
        sa.Column("checkpoint_status", sa.String(20)),
        sa.Column("checkpoints_completed", sa.Integer),
        sa.Column("checkpoints_failed", sa.Integer),
        sa.Column("backpressure_max", sa.Float),
        sa.Column("backpressure_detail", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("heap_used_bytes", sa.Integer),
        sa.Column("heap_max_bytes", sa.Integer),
        sa.Column("num_restarts", sa.Integer),
        sa.Column("watermark_lag_ms", sa.Integer),
    )
    op.create_index("ix_flink_metric_job_ts", "flink_metric_snapshots", ["job_id", "captured_at"])

    op.create_table("kafka_source_tables",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("org_id", sa.String(255), nullable=False),
        sa.Column("table_name", sa.String(255), nullable=False),
        sa.Column("kafka_topic", sa.String(500), nullable=False),
        sa.Column("format", sa.String(50), nullable=False, server_default="json"),
        sa.Column("consumer_group", sa.String(255)),
        sa.Column("ddl_sql", sa.Text, nullable=False),
        sa.Column("schema_json", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("org_id", "table_name", name="uq_kafka_src_org_name"),
    )

    op.create_table("iceberg_sink_tables",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("org_id", sa.String(255), nullable=False),
        sa.Column("table_name", sa.String(255), nullable=False),
        sa.Column("sink_type", sa.String(20), nullable=False, server_default="iceberg"),
        sa.Column("target_table", sa.String(500), nullable=False),
        sa.Column("ddl_sql", sa.Text, nullable=False),
        sa.Column("schema_json", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("org_id", "table_name", name="uq_iceberg_sink_org_name"),
    )

def downgrade() -> None:
    for t in ["iceberg_sink_tables", "kafka_source_tables", "flink_metric_snapshots",
              "flink_deployments", "flink_jobs"]:
        op.drop_table(t)
