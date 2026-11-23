"""Add dq_schema_tests column to projects; add openlineage config note.

Revision ID: 0002_dq_integration
Revises:     0001_initial
"""
from __future__ import annotations
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_dq_integration"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Store DQ-rendered schema.yml test blocks per project.
    # Each key is a target_table name; value is the full schema.yml YAML text.
    # Written to models/_dq_generated_tests.yml before each dbt run.
    op.add_column(
        "projects",
        sa.Column(
            "dq_schema_tests",
            postgresql.JSONB,
            nullable=True,
            comment="DQ-rendered schema.yml test blocks: {target_table: yaml_text}",
        ),
    )


def downgrade() -> None:
    op.drop_column("projects", "dq_schema_tests")
