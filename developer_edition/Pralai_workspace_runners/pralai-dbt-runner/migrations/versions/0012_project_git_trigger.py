"""DG-008: projects.git_trigger_enabled — "run on new commit", independent of the cron schedule.

Dagster's git_commit_sensor used to watch only the projects returned by the SCHEDULED listing, so a project could not be watched for
commits unless it also had a cron schedule (and every scheduled project was watched whether or not anyone wanted that). It now has its own
flag and its own listing (`GET /internal/projects/git-triggered`).

Existing projects keep exactly what they had: the flag is initialised from `schedule_enabled`, i.e. the projects the sensor was watching
yesterday are still watched today. New projects start with it off.

Revision ID: 0012_project_git_trigger
Revises:     0011_project_credential_ref
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012_project_git_trigger"
down_revision: Union[str, None] = "0011_project_credential_ref"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("git_trigger_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))
    # What the sensor watched until now: the schedule-enabled projects (with a cron).
    op.execute("UPDATE projects SET git_trigger_enabled = TRUE WHERE schedule_enabled IS TRUE AND schedule_cron IS NOT NULL")
    op.create_index("ix_projects_git_trigger", "projects", ["org_id"], postgresql_where=sa.text("git_trigger_enabled"))


def downgrade() -> None:
    op.drop_index("ix_projects_git_trigger", table_name="projects")
    op.drop_column("projects", "git_trigger_enabled")
