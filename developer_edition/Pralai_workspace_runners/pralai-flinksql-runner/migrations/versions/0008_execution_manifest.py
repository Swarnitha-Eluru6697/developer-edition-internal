"""X-004: flink_jobs.source and flink_deployments.manifest (+ hash, + immutability trigger).

A deployment used to remember only the sha256 of its SQL; the SQL it submitted was read from the mutable job row. Now the
deployment freezes a manifest (the SQL text, name, engine, parallelism, restore savepoint, subject, Git source and runtime
versions) and the submission reads only that. A trigger refuses to change `manifest` or `manifest_sha256` once set (a row
with none, created by an older runner, may have one set once). `flink_jobs.source` records the Git provenance of a job's
SQL (structured, instead of only a marker line in `description`).

Revision ID: 0008_execution_manifest
Revises:     0007_quality_runs_lifecycle
"""
from __future__ import annotations
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_execution_manifest"
down_revision: Union[str, None] = "0007_quality_runs_lifecycle"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("flink_jobs", sa.Column("source", postgresql.JSONB(astext_type=sa.Text())))
    op.add_column("flink_deployments", sa.Column("manifest", postgresql.JSONB(astext_type=sa.Text())))
    op.add_column("flink_deployments", sa.Column("manifest_sha256", sa.String(64)))
    op.execute("""
        CREATE FUNCTION flink_deployments_manifest_immutable() RETURNS trigger AS $$
        BEGIN
            IF OLD.manifest IS NOT NULL AND (
                   NEW.manifest        IS DISTINCT FROM OLD.manifest
                OR NEW.manifest_sha256 IS DISTINCT FROM OLD.manifest_sha256
                OR NEW.sql_hash        IS DISTINCT FROM OLD.sql_hash) THEN
                RAISE EXCEPTION 'flink_deployments.manifest, manifest_sha256 and sql_hash are immutable once set (deployment %)', OLD.id
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("CREATE TRIGGER trg_flink_deployments_manifest_immutable BEFORE UPDATE ON flink_deployments "
               "FOR EACH ROW EXECUTE FUNCTION flink_deployments_manifest_immutable()")


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_flink_deployments_manifest_immutable ON flink_deployments")
    op.execute("DROP FUNCTION flink_deployments_manifest_immutable()")
    op.drop_column("flink_deployments", "manifest_sha256")
    op.drop_column("flink_deployments", "manifest")
    op.drop_column("flink_jobs", "source")
