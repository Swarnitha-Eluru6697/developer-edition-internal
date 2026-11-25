"""pipeline revision (optimistic concurrency)

FE-BUG-14: dc_pipelines.revision is a plain integer counter, starting at 1,
bumped by update_pipeline() every time it actually applies a spec change —
see the Pipeline model's own docstring for why this is a *separate* column
from the existing `version` (the topic-identity part, e.g. "v1"). A client
that read a pipeline can send back the revision it saw as
PipelineUpdate.expected_revision; if the row has moved on since, the update
is rejected with a 409 instead of silently overwriting someone else's edit.

Revision ID: 0030_pipeline_revision
Revises: 0029_pipeline_runtime_state_last_observed_at
Create Date: 2026-09-24
"""

import sqlalchemy as sa

from alembic import op

revision = "0030_pipeline_revision"
down_revision = "0029_pipeline_runtime_state_last_observed_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "dc_pipelines",
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
    )


def downgrade() -> None:
    op.drop_column("dc_pipelines", "revision")
