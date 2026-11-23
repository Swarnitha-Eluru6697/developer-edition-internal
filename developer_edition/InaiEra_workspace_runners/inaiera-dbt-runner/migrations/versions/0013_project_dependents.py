"""Dependents: "if this project fails, these projects fail too" — a job link, independent of any data dependency.

project_dependents   one row per (upstream project, dependent project). Both sides are projects of the same org (checked by the
                     API); a project is never its own dependent; deleting either project removes the link.
runs.cancel_outcome  the status a stopped run ends in. NULL = 'cancelled' (a person cancelled it); 'failed' = it was stopped
                     because a project it depends on failed. Only read when a 'cancelling' run is finally confirmed stopped.

Existing projects start with no dependents, so nothing changes for them until someone adds one.

Revision ID: 0013_project_dependents
Revises:     0012_project_git_trigger
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0013_project_dependents"
down_revision: Union[str, None] = "0012_project_git_trigger"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "project_dependents",
        sa.Column("project_id", UUID(as_uuid=True), sa.ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("dependent_project_id", UUID(as_uuid=True), sa.ForeignKey("projects.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("project_id <> dependent_project_id", name="ck_project_dependent_not_self"),
    )
    op.create_index("ix_project_dependents_dependent", "project_dependents", ["dependent_project_id"])
    op.add_column("runs", sa.Column("cancel_outcome", sa.String(20), nullable=True))
    op.create_check_constraint("ck_run_cancel_outcome", "runs", "cancel_outcome IS NULL OR cancel_outcome = 'failed'")


def downgrade() -> None:
    op.drop_constraint("ck_run_cancel_outcome", "runs", type_="check")
    op.drop_column("runs", "cancel_outcome")
    op.drop_index("ix_project_dependents_dependent", table_name="project_dependents")
    op.drop_table("project_dependents")
