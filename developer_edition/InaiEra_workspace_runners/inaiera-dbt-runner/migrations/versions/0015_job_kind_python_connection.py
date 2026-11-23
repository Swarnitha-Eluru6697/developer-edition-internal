"""Jobs runner: Python jobs, and SQL jobs that run on a Workspace connection.

projects.kind           now 'dbt' | 'sql' | 'python' (a .py script: its own virtualenv from the requirements.txt beside it).
projects.connection_id  a SQL job only: the Workspace database connection (Database Explorer) it runs on. NULL = the runner's
                        warehouse engine. The connection's credentials never reach the runner: it asks the Workspace to run the
                        statements (app/executors/connection_sql_job.py).

Revision ID: 0015_job_kind_python_conn
Revises:     0014_project_kind
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0015_job_kind_python_conn"
down_revision: Union[str, None] = "0014_project_kind"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("ck_project_kind", "projects", type_="check")
    op.create_check_constraint("ck_project_kind", "projects", "kind IN ('dbt', 'sql', 'python')")
    op.add_column("projects", sa.Column("connection_id", UUID(as_uuid=True), nullable=True))
    op.create_check_constraint("ck_project_connection_sql_only", "projects", "connection_id IS NULL OR kind = 'sql'")


def downgrade() -> None:
    op.drop_constraint("ck_project_connection_sql_only", "projects", type_="check")
    op.drop_column("projects", "connection_id")
    op.execute("DELETE FROM projects WHERE kind = 'python'")      # an older schema has no such job; its runs go with it (CASCADE)
    op.drop_constraint("ck_project_kind", "projects", type_="check")
    op.create_check_constraint("ck_project_kind", "projects", "kind IN ('dbt', 'sql')")
