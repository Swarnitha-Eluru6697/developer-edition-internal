"""SQL jobs on a connection: which Workspace connection a registered dbt-runner SQL job runs on, and as whom.

`job_connection_bindings(project_id, connection_id, user_id, roles)`; see app.models.JobConnectionBinding. Rows go with their
connection or user (ON DELETE CASCADE) and with their job (the dbt proxy deletes them when the job is deleted).

Revision ID: 0014_job_connection_bindings
Revises: 0013_stable_identity_prov
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0014_job_connection_bindings"
down_revision: Union[str, None] = "0013_stable_identity_prov"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "job_connection_bindings",
        sa.Column("project_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("connection_id", UUID(as_uuid=True), sa.ForeignKey("database_connections.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("roles", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_job_conn_bindings_connection", "job_connection_bindings", ["connection_id"])


def downgrade() -> None:
    op.drop_index("ix_job_conn_bindings_connection", table_name="job_connection_bindings")
    op.drop_table("job_connection_bindings")
