"""pipeline schema introspection status

BUG-10: PipelineService.autocreate_schema_from_source's outcome (linked an
existing schema, auto-created one, skipped because the source had nothing
to introspect, or failed with an unexpected error such as a connection
timeout) was previously invisible — the create_pipeline route's
`except Exception: pass` discarded it entirely. These two columns give it
somewhere to live; see app/services/pipeline_service.py.

Revision ID: 0024_pipeline_schema_introspection_status
Revises: 0023_pipeline_runtime_state_lease
Create Date: 2026-09-22
"""

import sqlalchemy as sa

from alembic import op

revision = "0024_pipeline_schema_introspection_status"
down_revision = "0023_pipeline_runtime_state_lease"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("dc_pipelines", sa.Column("schema_introspection_status", sa.String(length=20), nullable=True))
    op.add_column("dc_pipelines", sa.Column("schema_introspection_message", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("dc_pipelines", "schema_introspection_message")
    op.drop_column("dc_pipelines", "schema_introspection_status")
