"""DBT-006/007: runs.manifest — what a run executes is frozen when it is submitted.

A queued run used to read the project's Git ref and DQ schema blocks only when it was finally dispatched, so a branch
that advanced (or DQ rules that were replaced) while it waited changed what it built. `manifest` holds the resolved
commit and the DQ blocks with their artifact versions and hashes; `manifest_sha256` is its hash. A trigger refuses to
change either, or `git_sha`, once the manifest is set. A row with no manifest (queued by an older runner version) may
have one set exactly once.

Revision ID: 0009_run_manifest
Revises:     0008_run_cancelling
"""
from __future__ import annotations
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_run_manifest"
down_revision: Union[str, None] = "0008_run_cancelling"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("manifest", postgresql.JSONB(astext_type=sa.Text())))
    op.add_column("runs", sa.Column("manifest_sha256", sa.String(64)))
    op.execute("""
        CREATE FUNCTION runs_manifest_immutable() RETURNS trigger AS $$
        BEGIN
            IF OLD.manifest IS NOT NULL AND (
                   NEW.manifest        IS DISTINCT FROM OLD.manifest
                OR NEW.manifest_sha256 IS DISTINCT FROM OLD.manifest_sha256
                OR NEW.git_sha         IS DISTINCT FROM OLD.git_sha) THEN
                RAISE EXCEPTION 'runs.manifest, manifest_sha256 and git_sha are immutable once set (run %)', OLD.id
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("CREATE TRIGGER trg_runs_manifest_immutable BEFORE UPDATE ON runs "
               "FOR EACH ROW EXECUTE FUNCTION runs_manifest_immutable()")


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_runs_manifest_immutable ON runs")
    op.execute("DROP FUNCTION runs_manifest_immutable()")
    op.drop_column("runs", "manifest_sha256")
    op.drop_column("runs", "manifest")
