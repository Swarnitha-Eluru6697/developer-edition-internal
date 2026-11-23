"""Allow 'python' as a codegen target.

Revision ID: 0004_codegen_python_target
Revises: 0003_db_connections
Create Date: 2026-07-21
"""
from __future__ import annotations
from typing import Sequence, Union
from alembic import op

revision: str = "0004_codegen_python_target"
down_revision: Union[str, None] = "0003_db_connections"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("ck_codegen_target", "codegen_sessions", type_="check")
    op.create_check_constraint(
        "ck_codegen_target",
        "codegen_sessions",
        "target IN ('sql', 'python', 'dbt', 'flinksql')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_codegen_target", "codegen_sessions", type_="check")
    op.create_check_constraint(
        "ck_codegen_target",
        "codegen_sessions",
        "target IN ('sql', 'dbt', 'flinksql')",
    )
