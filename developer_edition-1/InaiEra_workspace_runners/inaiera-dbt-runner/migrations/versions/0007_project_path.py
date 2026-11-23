"""project_path: several dbt projects (folders) in one Git repo.

A registered project is now unique per (org, git_url, git_ref, project_path).
Existing rows get project_path = '' (repo root / the only dbt_project.yml), so
their uniqueness and behaviour are unchanged.

Revision ID: 0007_project_path
Revises:     0006_dq_schema_tests_meta
"""
from __future__ import annotations
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op

revision: str = "0007_project_path"
down_revision: Union[str, None] = "0006_dq_schema_tests_meta"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("project_path", sa.String(512), nullable=False, server_default=""))
    op.drop_constraint("uq_project_repo_ref", "projects", type_="unique")
    op.create_unique_constraint(
        "uq_project_repo_ref", "projects", ["org_id", "git_url", "git_ref", "project_path"])


def downgrade() -> None:
    # Only safe while no repo has several registered folders.
    op.drop_constraint("uq_project_repo_ref", "projects", type_="unique")
    op.create_unique_constraint("uq_project_repo_ref", "projects", ["org_id", "git_url", "git_ref"])
    op.drop_column("projects", "project_path")
