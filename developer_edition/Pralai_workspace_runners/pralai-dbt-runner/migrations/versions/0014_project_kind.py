"""Jobs runner: a registered project is a dbt project or a SQL job.

projects.kind   'dbt' (a dbt project folder; every existing row) or 'sql' (one .sql file of the repo, run statement by statement
                against the project's engine). Same registry, runs, schedules, Git trigger, Dependents and Dagster jobs for both.

Revision ID: 0014_project_kind
Revises:     0013_project_dependents
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0014_project_kind"
down_revision: Union[str, None] = "0013_project_dependents"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("kind", sa.String(10), nullable=False, server_default="dbt"))
    op.create_check_constraint("ck_project_kind", "projects", "kind IN ('dbt', 'sql')")


def downgrade() -> None:
    op.drop_constraint("ck_project_kind", "projects", type_="check")
    op.drop_column("projects", "kind")
