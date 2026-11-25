"""Add job_kind and ucs_fqn to flink_jobs.

Revision ID: 0002_quality_fields
Revises:     0001_initial
"""
from __future__ import annotations
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op

revision: str = "0002_quality_fields"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # job_kind distinguishes transform jobs (the main ETL pipeline) from
    # quality jobs (parallel checks submitted by the DQ Service).
    op.add_column(
        "flink_jobs",
        sa.Column(
            "job_kind",
            sa.String(20),
            nullable=False,
            server_default="transform",
            comment="transform | quality",
        ),
    )
    op.create_check_constraint(
        "ck_flink_job_kind",
        "flink_jobs",
        "job_kind IN ('transform', 'quality')",
    )

    # ucs_fqn links a quality job to the UCS dataset it reports results under.
    # Set by the DQ Service when it submits the quality job via POST /api/v1/jobs.
    op.add_column(
        "flink_jobs",
        sa.Column(
            "ucs_fqn",
            sa.String(500),
            nullable=True,
            comment="UCS dataset FQN for quality result reporting (quality jobs only)",
        ),
    )


def downgrade() -> None:
    op.drop_constraint("ck_flink_job_kind", "flink_jobs")
    op.drop_column("flink_jobs", "ucs_fqn")
    op.drop_column("flink_jobs", "job_kind")
