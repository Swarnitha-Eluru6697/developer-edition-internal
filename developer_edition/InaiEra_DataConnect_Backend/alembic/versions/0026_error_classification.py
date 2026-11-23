"""error classification

BUG-11: both background runners (Operation, QualityExecutionJob) used to
persist `str(exc)[:2000]` as error_message — a database/HTTP/Kafka driver's
own exception text can carry a username, hostname, URL, SQL, a file path,
or a request payload. app/services/safe_error.py now classifies every
failure into a small set of generic, non-leaking messages before anything
is persisted; these columns carry the rest of that classification
(error_message itself stays the existing column, now always safe).

Revision ID: 0026_error_classification
Revises: 0025_discovery_run_and_objects
Create Date: 2026-09-22
"""

import sqlalchemy as sa

from alembic import op

revision = "0026_error_classification"
down_revision = "0025_discovery_run_and_objects"
branch_labels = None
depends_on = None

_TABLES = ("dc_operations", "dc_quality_execution_jobs")


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(table, sa.Column("error_code", sa.String(length=100), nullable=True))
        op.add_column(table, sa.Column("retryable", sa.Boolean(), nullable=True))
        op.add_column(table, sa.Column("correlation_id", sa.String(length=64), nullable=True))
        op.create_index(f"ix_{table}_correlation_id", table, ["correlation_id"])


def downgrade() -> None:
    for table in _TABLES:
        op.drop_index(f"ix_{table}_correlation_id", table_name=table)
        op.drop_column(table, "correlation_id")
        op.drop_column(table, "retryable")
        op.drop_column(table, "error_code")
