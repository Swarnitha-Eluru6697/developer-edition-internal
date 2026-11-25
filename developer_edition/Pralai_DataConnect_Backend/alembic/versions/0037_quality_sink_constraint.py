"""quality policy quarantine_sink check constraint

BUG-09: the API already rejects quarantine_sink='object_storage' (a reserved value with no backend), but nothing
stopped a direct SQL write or a path that skips validation from storing it. This makes the database refuse it too.
It refuses to run if a row ALREADY holds an unimplemented sink — it will not silently rewrite someone's policy; fix
those rows (set quarantine_sink='kafka') and re-run.

Widening the allowed list when a sink is implemented needs a new migration that drops and recreates this constraint.

Revision ID: 0037_quality_sink_constraint
Revises: 0036_connection_verifications_table
Create Date: 2026-10-01
"""

import sqlalchemy as sa

from alembic import op

revision = "0037_quality_sink_constraint"
down_revision = "0036_connection_verifications_table"
branch_labels = None
depends_on = None

_NAME = "ck_dc_quality_policies_quarantine_sink_implemented"


def upgrade() -> None:
    bad = op.get_bind().execute(
        sa.text("SELECT id, name, quarantine_sink FROM dc_quality_policies WHERE quarantine_sink NOT IN ('kafka')")
    ).fetchall()
    if bad:
        listing = ", ".join(f"{row.name!r} ({row.id}) uses {row.quarantine_sink!r}" for row in bad)
        raise RuntimeError(
            "Cannot add the quarantine_sink constraint: these policies use a sink with no backend — "
            f"set them to 'kafka' first: {listing}"
        )
    op.create_check_constraint(_NAME, "dc_quality_policies", "quarantine_sink IN ('kafka')")


def downgrade() -> None:
    op.drop_constraint(_NAME, "dc_quality_policies", type_="check")
