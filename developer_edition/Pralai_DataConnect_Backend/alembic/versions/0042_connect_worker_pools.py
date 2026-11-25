"""Kafka Connect worker pools: every connector template, pipeline and runtime-state row carries its pool

Runtime design: connectors run in separate Connect worker pools by workload (cdc / jdbc / file / api / stream / sink).
Until now `runtime_pool_id` existed on pipelines but was never set (NULL on every row), the queue pool was a retired name
for the stream pool, and nothing could route a connector to a pool-specific cluster. This migration:

  1. renames the retired `connect-queue-pool` to `connect-stream-pool` on connector templates, and puts every SINK template
     in `connect-sink-pool` (the MongoDB sink had been placed in the JDBC pool — a sink belongs with the sinks);
  2. sets every pipeline's `runtime_pool_id` from its template (the pool is derived from the template, never chosen);
  3. adds `runtime_pool_id` to the runtime-state rows, so the reconciler can reach the right cluster for a connector whose
     pipeline row is gone, and copies it from the pipeline;
  4. constrains pipelines' `runtime_pool_id` to the six known pools (or NULL).

Revision ID: 0042_connect_worker_pools
Revises: 0041_quality_job_chunks
Create Date: 2026-10-02
"""

import sqlalchemy as sa
from alembic import op

revision = "0042_connect_worker_pools"
down_revision = "0041_quality_job_chunks"
branch_labels = None
depends_on = None

_POOLS = (
    "'connect-cdc-pool', 'connect-jdbc-pool', 'connect-file-pool', 'connect-api-pool', 'connect-stream-pool', "
    "'connect-sink-pool'"
)


def upgrade() -> None:
    op.execute("UPDATE dc_connector_templates SET runtime_pool_type = 'connect-stream-pool' WHERE runtime_pool_type = 'connect-queue-pool'")
    op.execute("UPDATE dc_connector_templates SET runtime_pool_type = 'connect-sink-pool' WHERE connector_type = 'sink' AND runtime_pool_type <> 'connect-sink-pool'")
    op.execute(
        """
        UPDATE dc_pipelines p SET runtime_pool_id = t.runtime_pool_type
        FROM dc_connector_templates t
        WHERE t.id::text = p.template_id::text
          AND (p.runtime_pool_id IS NULL OR p.runtime_pool_id = 'connect-queue-pool' OR p.runtime_pool_id <> t.runtime_pool_type)
        """
    )
    op.execute("UPDATE dc_pipelines SET runtime_pool_id = 'connect-stream-pool' WHERE runtime_pool_id = 'connect-queue-pool'")
    op.add_column("dc_pipeline_runtime_state", sa.Column("runtime_pool_id", sa.String(255), nullable=True))
    op.execute(
        """
        UPDATE dc_pipeline_runtime_state r SET runtime_pool_id = p.runtime_pool_id
        FROM dc_pipelines p WHERE p.id::text = r.pipeline_id
        """
    )
    bad = bind_scalar("SELECT count(*) FROM dc_pipelines WHERE runtime_pool_id IS NOT NULL AND runtime_pool_id NOT IN (" + _POOLS + ")")
    if bad:
        raise RuntimeError(
            f"{bad} pipeline(s) name a worker pool that does not exist; fix their runtime_pool_id (or their template's "
            "runtime_pool_type) before adding the constraint."
        )
    op.create_check_constraint(
        "ck_dc_pipelines_runtime_pool", "dc_pipelines", f"runtime_pool_id IS NULL OR runtime_pool_id IN ({_POOLS})",
    )


def bind_scalar(query: str) -> int:
    return int(op.get_bind().execute(sa.text(query)).scalar() or 0)


def downgrade() -> None:
    op.drop_constraint("ck_dc_pipelines_runtime_pool", "dc_pipelines", type_="check")
    op.drop_column("dc_pipeline_runtime_state", "runtime_pool_id")
    op.execute("UPDATE dc_connector_templates SET runtime_pool_type = 'connect-queue-pool' WHERE runtime_pool_type = 'connect-stream-pool'")
