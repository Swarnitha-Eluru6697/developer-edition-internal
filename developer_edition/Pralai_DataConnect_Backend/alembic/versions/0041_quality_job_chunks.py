"""quality job input is staged in chunks, with a per-chunk checkpoint

GAP-13. A job's records were staged as ONE row holding the whole batch, which the worker read back whole. Now a batch is
stored as ordered chunks (one row each; a legacy single-row batch is simply chunk 0) and the job row carries
`chunk_count` and a `progress` checkpoint, so a worker streams one chunk at a time and a reclaimed job resumes after the
last chunk it finished.

Revision ID: 0041_quality_job_chunks
Revises: 0040_pipeline_direction_integrity
Create Date: 2026-10-01
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0041_quality_job_chunks"
down_revision = "0040_pipeline_direction_integrity"
branch_labels = None
depends_on = None

_TABLE = "dc_quality_execution_job_records"


def upgrade() -> None:
    op.add_column("dc_quality_execution_jobs", sa.Column("chunk_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("dc_quality_execution_jobs", sa.Column("progress", postgresql.JSONB(), nullable=True))
    op.add_column(_TABLE, sa.Column("chunk_index", sa.Integer(), nullable=False, server_default="0"))
    op.add_column(_TABLE, sa.Column("byte_size", sa.Integer(), nullable=False, server_default="0"))
    # job_id was unique (one row per job); it is now unique per (job_id, chunk_index).
    op.drop_index("ix_dc_quality_execution_job_records_job_id", table_name=_TABLE)
    op.create_index("ix_dc_quality_execution_job_records_job_id", _TABLE, ["job_id"])
    op.create_unique_constraint("uq_dc_quality_execution_job_records_job_chunk", _TABLE, ["job_id", "chunk_index"])


def downgrade() -> None:
    # Only a single-chunk batch fits the old one-row-per-job shape: keep each job's first chunk.
    op.execute(
        f"DELETE FROM {_TABLE} r USING {_TABLE} keep WHERE r.job_id = keep.job_id AND r.chunk_index > keep.chunk_index AND keep.chunk_index = 0"
    )
    op.drop_constraint("uq_dc_quality_execution_job_records_job_chunk", _TABLE, type_="unique")
    op.drop_index("ix_dc_quality_execution_job_records_job_id", table_name=_TABLE)
    op.create_index("ix_dc_quality_execution_job_records_job_id", _TABLE, ["job_id"], unique=True)
    op.drop_column(_TABLE, "byte_size")
    op.drop_column(_TABLE, "chunk_index")
    op.drop_column("dc_quality_execution_jobs", "progress")
    op.drop_column("dc_quality_execution_jobs", "chunk_count")
