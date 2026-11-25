"""DQ apply metadata per stored schema-tests block (ApplyDqArtifact).

ADDITIVE ONLY: one nullable JSONB column; existing rows unaffected.

Revision ID: 0006_dq_schema_tests_meta
Revises:     0005_run_leases
"""
from __future__ import annotations
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_dq_schema_tests_meta"
down_revision: Union[str, None] = "0005_run_leases"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("dq_schema_tests_meta", postgresql.JSONB))


def downgrade() -> None:
    op.drop_column("projects", "dq_schema_tests_meta")
