"""node-secret retirement; per-file template policy

Revision ID: d95a3f1b7c62
Revises: c84f2e0d6b51
Create Date: 2026-10-20 21:30:00.000000

* agentnodesecret.retired_at: a secret the draft stopped declaring while a
  published version still does is kept (retired) instead of deleted.
* agentfile.template_policy: require (default) | embed | exclude.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "d95a3f1b7c62"
down_revision: Union[str, Sequence[str], None] = "c84f2e0d6b51"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("agentnodesecret", sa.Column("retired_at", sa.DateTime(), nullable=True))
    op.add_column("agentfile", sa.Column("template_policy", sa.String(), nullable=False, server_default="require"))


def downgrade() -> None:
    op.drop_column("agentfile", "template_policy")
    op.drop_column("agentnodesecret", "retired_at")
