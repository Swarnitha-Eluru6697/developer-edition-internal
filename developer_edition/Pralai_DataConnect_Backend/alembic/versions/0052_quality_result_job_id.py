"""A quality execution job has exactly one result

NEW-05: a chunked job commits its QualityResult and only afterwards is marked `succeeded`. A crash in between made the reclaiming worker finalize
the job again and write a second result for it (and emit it to UCS twice). `job_id` records which job a result summarises and is UNIQUE, so the
second attempt finds the first one instead. Nullable: a result that did not come from a job (a synchronous preview or execute) has none, and
existing rows keep NULL. No foreign key: jobs are pruned over time and the result must outlive them.

Revision ID: 0052_quality_result_job_id
Revises: 0051_quality_stream_offsets
Create Date: 2026-10-13
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0052_quality_result_job_id"
down_revision = "0051_quality_stream_offsets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("dc_quality_results", sa.Column("job_id", postgresql.UUID(as_uuid=False), nullable=True))
    op.create_unique_constraint("uq_dc_quality_results_job_id", "dc_quality_results", ["job_id"])


def downgrade() -> None:
    op.drop_constraint("uq_dc_quality_results_job_id", "dc_quality_results", type_="unique")
    op.drop_column("dc_quality_results", "job_id")
