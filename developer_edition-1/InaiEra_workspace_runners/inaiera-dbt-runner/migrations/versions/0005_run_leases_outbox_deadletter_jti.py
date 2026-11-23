"""Run ownership leases + per-attempt k8s resources (RUN-DBT-003), outbox
dead-letter/skipped statuses (RUN-DBT-008), scoped service-token jti replay
table (RUN-DBT-005).

Revision ID: 0005_run_leases
Revises:     0004_metadata_outbox
"""
from __future__ import annotations
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op

revision: str = "0005_run_leases"
down_revision: Union[str, None] = "0004_metadata_outbox"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("attempt", sa.Integer, nullable=False, server_default="0"))
    op.add_column("runs", sa.Column("lease_owner", sa.String(255)))
    op.add_column("runs", sa.Column("lease_expires_at", sa.DateTime(timezone=True)))
    op.add_column("runs", sa.Column("heartbeat_at", sa.DateTime(timezone=True)))
    op.add_column("runs", sa.Column("k8s_job_name", sa.String(63)))
    op.add_column("runs", sa.Column("job_released_at", sa.DateTime(timezone=True)))
    op.add_column("runs", sa.Column("result_ref", sa.Text))

    op.drop_constraint("ck_metadata_outbox_status", "metadata_outbox_events", type_="check")
    op.create_check_constraint(
        "ck_metadata_outbox_status", "metadata_outbox_events",
        "status IN ('pending', 'delivered', 'dead_letter', 'skipped')",
    )

    op.create_table(
        "service_token_jti",
        sa.Column("jti", sa.String(255), primary_key=True),
        sa.Column("issuer", sa.String(255), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_service_token_jti_expires", "service_token_jti", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_service_token_jti_expires", table_name="service_token_jti")
    op.drop_table("service_token_jti")

    op.execute("UPDATE metadata_outbox_events SET status = 'pending' "
               "WHERE status IN ('dead_letter', 'skipped')")
    op.drop_constraint("ck_metadata_outbox_status", "metadata_outbox_events", type_="check")
    op.create_check_constraint(
        "ck_metadata_outbox_status", "metadata_outbox_events",
        "status IN ('pending', 'delivered')",
    )

    for col in ("result_ref", "job_released_at", "k8s_job_name", "heartbeat_at",
                "lease_expires_at", "lease_owner", "attempt"):
        op.drop_column("runs", col)
