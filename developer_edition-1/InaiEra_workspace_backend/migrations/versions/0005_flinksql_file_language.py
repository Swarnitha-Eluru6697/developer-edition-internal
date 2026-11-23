"""Allow 'flinksql' as a workspace file language.

FlinkSQL is authored as a normal workspace file (*.flink.sql) in the same
editor as .sql, but it targets the FlinkSQL Runner rather than the lakehouse
SQL executor — so it needs its own language value, not a reuse of 'sql'.

Revision ID: 0005_flinksql_file_language
Revises: 0004_codegen_python_target
Create Date: 2026-07-27
"""
from __future__ import annotations
from typing import Sequence, Union
from alembic import op

revision: str = "0005_flinksql_file_language"
down_revision: Union[str, None] = "0004_codegen_python_target"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("ck_file_language", "workspace_files", type_="check")
    op.create_check_constraint(
        "ck_file_language",
        "workspace_files",
        "language IN ('sql','flinksql','ipynb','py','md','yaml','json','other')",
    )


def downgrade() -> None:
    # Existing flinksql rows would violate the narrower constraint — fold them
    # back into plain sql first (the *.flink.sql path still identifies them).
    op.execute("UPDATE workspace_files SET language = 'sql' WHERE language = 'flinksql'")
    op.drop_constraint("ck_file_language", "workspace_files", type_="check")
    op.create_check_constraint(
        "ck_file_language",
        "workspace_files",
        "language IN ('sql','ipynb','py','md','yaml','json','other')",
    )
