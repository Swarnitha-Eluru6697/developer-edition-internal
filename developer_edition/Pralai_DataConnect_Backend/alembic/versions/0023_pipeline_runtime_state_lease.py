"""pipeline runtime state lease

GAP-08: more than one RuntimeReconciler process can run
`run_forever()` at once with nothing coordinating them today — every
process reconciles every pipeline, every tick, unconditionally. This adds
the claim/lease columns app/services/runtime_reconciler.py's atomic claim
query uses so only one live process owns (and acts on) a given pipeline at
a time, and ownership transfers to another live process once a dead one's
lease lapses (mirrors 0018_operation_claim_lease.py's Operation lease,
adapted for a row that's claimed/renewed forever rather than once).

Revision ID: 0023_pipeline_runtime_state_lease
Revises: 0022_connector_template_supported_modes
Create Date: 2026-09-22
"""

import sqlalchemy as sa

from alembic import op

revision = "0023_pipeline_runtime_state_lease"
down_revision = "0022_connector_template_supported_modes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("dc_pipeline_runtime_state", sa.Column("leased_by", sa.String(length=200), nullable=True))
    op.add_column(
        "dc_pipeline_runtime_state", sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index(
        "ix_dc_pipeline_runtime_state_leased_by", "dc_pipeline_runtime_state", ["leased_by"],
    )
    op.create_index(
        "ix_dc_pipeline_runtime_state_lease_expires_at", "dc_pipeline_runtime_state", ["lease_expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_dc_pipeline_runtime_state_lease_expires_at", table_name="dc_pipeline_runtime_state")
    op.drop_index("ix_dc_pipeline_runtime_state_leased_by", table_name="dc_pipeline_runtime_state")
    op.drop_column("dc_pipeline_runtime_state", "lease_expires_at")
    op.drop_column("dc_pipeline_runtime_state", "leased_by")
