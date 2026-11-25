"""pipeline history fk ondelete

Revision ID: 0014_pipeline_history_fk_ondelete
Revises: 0013_operations
Create Date: 2026-09-17

Every history/audit table that references dc_pipelines.id was created with
the implicit RESTRICT foreign key behavior, which permanently blocks
deleting a pipeline the instant any such row exists for it — e.g. a single
deploy already materializes dc_lineage_edges rows, so no deployed pipeline
could ever be deleted afterward; the reconciler would retry the delete every
tick forever, always hitting the same IntegrityError.

dc_lineage_edges.pipeline_id and dc_quality_results.pipeline_id are already
nullable (both documented as pipeline history, not something the pipeline
owns), so they move to ON DELETE SET NULL. dc_pipeline_runs.pipeline_id and
dc_quality_execution_jobs.pipeline_id are NOT NULL, so SET NULL isn't an
option there; those move to ON DELETE CASCADE instead, since a run/job
record has no meaning once its pipeline is gone.
"""

from alembic import op

revision = "0014_pipeline_history_fk_ondelete"
down_revision = "0013_operations"
branch_labels = None
depends_on = None

_SET_NULL = [
    ("dc_lineage_edges_pipeline_id_fkey", "dc_lineage_edges"),
    ("dc_quality_results_pipeline_id_fkey", "dc_quality_results"),
]
_CASCADE = [
    ("dc_pipeline_runs_pipeline_id_fkey", "dc_pipeline_runs"),
    ("dc_quality_execution_jobs_pipeline_id_fkey", "dc_quality_execution_jobs"),
]


def upgrade() -> None:
    for constraint_name, table in _SET_NULL:
        op.drop_constraint(constraint_name, table, type_="foreignkey")
        op.create_foreign_key(
            constraint_name, table, "dc_pipelines", ["pipeline_id"], ["id"], ondelete="SET NULL"
        )
    for constraint_name, table in _CASCADE:
        op.drop_constraint(constraint_name, table, type_="foreignkey")
        op.create_foreign_key(
            constraint_name, table, "dc_pipelines", ["pipeline_id"], ["id"], ondelete="CASCADE"
        )


def downgrade() -> None:
    for constraint_name, table in _SET_NULL + _CASCADE:
        op.drop_constraint(constraint_name, table, type_="foreignkey")
        op.create_foreign_key(constraint_name, table, "dc_pipelines", ["pipeline_id"], ["id"])
