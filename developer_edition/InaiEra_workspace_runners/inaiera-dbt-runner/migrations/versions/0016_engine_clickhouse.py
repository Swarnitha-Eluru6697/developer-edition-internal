"""ClickHouse as a third engine for dbt, SQL and Python jobs.

projects.engine  now 'trino' | 'pg_lake' | 'clickhouse' (the CHECK constraint ck_project_engine is widened; no data changes).

Revision ID: 0016_engine_clickhouse
Revises:     0015_job_kind_python_conn
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0016_engine_clickhouse"
down_revision: Union[str, None] = "0015_job_kind_python_conn"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("ck_project_engine", "projects", type_="check")
    op.create_check_constraint("ck_project_engine", "projects", "engine IN ('trino', 'pg_lake', 'clickhouse')")


def downgrade() -> None:
    op.execute("DELETE FROM projects WHERE engine = 'clickhouse'")      # an older schema has no such job; its runs go with it (CASCADE)
    op.drop_constraint("ck_project_engine", "projects", type_="check")
    op.create_check_constraint("ck_project_engine", "projects", "engine IN ('trino', 'pg_lake')")
