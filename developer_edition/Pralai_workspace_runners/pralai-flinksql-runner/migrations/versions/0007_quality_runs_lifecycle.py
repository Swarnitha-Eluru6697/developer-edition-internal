"""Quality contract v2: outbox `kind`, dead-letter/skipped statuses, and
per-check heartbeats for the QualityRun lifecycle (DQ-RESULT-009/010/011).

ADDITIVE ONLY — safe on a live database at 0006:
  * quality_result_outbox.kind is NOT NULL with server default
    'quality_run', so existing rows are backfilled by the default;
  * the status CHECK is widened (adds 'rejected', 'skipped'); existing
    'pending'/'delivered' rows remain valid;
  * quality_check_heartbeats is a new table.

Revision ID: 0007_quality_runs_lifecycle
Revises:     0006_lifecycle_leases_audit
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007_quality_runs_lifecycle"
down_revision: Union[str, None] = "0006_lifecycle_leases_audit"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("quality_result_outbox",
                  sa.Column("kind", sa.String(30), nullable=False, server_default="quality_run"))
    op.drop_constraint("ck_quality_outbox_status", "quality_result_outbox", type_="check")
    op.create_check_constraint(
        "ck_quality_outbox_status", "quality_result_outbox",
        "status IN ('pending', 'delivered', 'rejected', 'skipped')")
    op.create_table(
        "quality_check_heartbeats",
        sa.Column("org_id", sa.String(255), primary_key=True),
        sa.Column("ucs_fqn", sa.String(500), primary_key=True),
        sa.Column("check_id", sa.String(64), primary_key=True),
        sa.Column("run_ref", sa.String(255), primary_key=True),
        sa.Column("binding_id", sa.String(64)),
        sa.Column("rule_name", sa.String(255)),
        sa.Column("window_seconds", sa.Integer, nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("last_window_end", sa.String(40)),
        sa.Column("state", sa.String(20), nullable=False),
        sa.CheckConstraint("state IN ('active', 'stale')", name="ck_quality_heartbeat_state"),
    )
    op.create_index("ix_quality_heartbeat_state_seen", "quality_check_heartbeats",
                    ["state", "last_seen_at"])


def downgrade() -> None:
    op.drop_index("ix_quality_heartbeat_state_seen", table_name="quality_check_heartbeats")
    op.drop_table("quality_check_heartbeats")
    op.execute("DELETE FROM quality_result_outbox WHERE status IN ('rejected', 'skipped')")
    op.drop_constraint("ck_quality_outbox_status", "quality_result_outbox", type_="check")
    op.create_check_constraint("ck_quality_outbox_status", "quality_result_outbox",
                               "status IN ('pending', 'delivered')")
    op.drop_column("quality_result_outbox", "kind")
