"""make run and trace legacy display columns nullable, add status check constraints

Revision ID: 8abfb57664d5
Revises: e16cf5041c0f
Create Date: 2026-09-06 16:55:17.455702

§0.116, GAP_REPORT — the actual fix for a real, reported gap: Run and
Trace carried both real, typed fields (duration_ms, cost_usd,
started_at, completed_at) and legacy, independently-computed display
STRINGS (duration, cost, ts) — two real, separate computations that
happened to agree today with no real, structural guarantee of staying
that way. The fix makes the typed fields canonical: `duration`/`cost`/
`ts` are now real, computed Pydantic properties (models.py), derived
from the typed fields at response-serialization time — never an
independently-settable value again. The underlying database columns
are kept (renamed at the Python attribute level only, to
duration_legacy/cost_legacy/ts_legacy — mapped via an explicit
sa_column onto the SAME, unchanged real column names, so no column
RENAME is needed at the database level at all) so every real, existing
row's own historical display value is preserved and still used as a
real, honest fallback for a row that predates the typed fields.

This migration's own, real, necessary half: the real, original columns
were NOT NULL (duration/cost/ts were required `str` fields on the old
model) — confirmed directly, via a live IntegrityError, that a NEW row
inserted under the updated model (which no longer sets these columns
at all) would violate that real, existing constraint. Relaxes all
five real columns (run.duration, run.cost, run.ts, trace.duration,
trace.ts) to nullable, and adds the report's own second, explicit ask
— a real, database-level CHECK constraint enforcing Run/Trace's own
real, enumerated status values (see models.py's own RunStatus/
TraceStatus for the full real reasoning on the exact set chosen).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = '8abfb57664d5'
down_revision: Union[str, Sequence[str], None] = 'e16cf5041c0f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column('run', 'duration', existing_type=sa.VARCHAR(), nullable=True)
    op.alter_column('run', 'cost', existing_type=sa.VARCHAR(), nullable=True)
    op.alter_column('run', 'ts', existing_type=sa.VARCHAR(), nullable=True)
    op.alter_column('trace', 'duration', existing_type=sa.VARCHAR(), nullable=True)
    op.alter_column('trace', 'ts', existing_type=sa.VARCHAR(), nullable=True)

    op.create_check_constraint(
        'ck_run_status_enum', 'run',
        "status IN ('queued','running','paused','success','failed','blocked','cancelled','denied')",
    )
    op.create_check_constraint(
        'ck_trace_status_enum', 'trace',
        "status IN ('ok','ucs_denial','harness_fail','orch_fail','paused','moderation_blocked')",
    )


def downgrade() -> None:
    """Downgrade schema.

    A real, honest, stated limitation: by the time a real downgrade
    might run, real NEW rows may already have genuinely NULL duration/
    cost/ts (this fix's own new code no longer ever sets them) — a
    downgrade back to NOT NULL would genuinely fail against any such
    real row with a real, live NotNullViolation. Backfills a real,
    honestly-labeled placeholder into any currently-NULL row for
    exactly these five columns FIRST, so the constraint re-add below
    can genuinely succeed — matching this platform's own established
    "no fabricated value passed off as real historical data" stance:
    the placeholder itself states plainly that it's a downgrade
    artifact, not a real, recovered original value that was never
    actually stored.
    """
    op.drop_constraint('ck_trace_status_enum', 'trace', type_='check')
    op.drop_constraint('ck_run_status_enum', 'run', type_='check')

    op.execute("UPDATE run SET duration = '[unknown — downgraded from typed fields]' WHERE duration IS NULL")
    op.execute("UPDATE run SET cost = '[unknown — downgraded from typed fields]' WHERE cost IS NULL")
    op.execute("UPDATE run SET ts = '[unknown — downgraded from typed fields]' WHERE ts IS NULL")
    op.execute("UPDATE trace SET duration = '[unknown — downgraded from typed fields]' WHERE duration IS NULL")
    op.execute("UPDATE trace SET ts = '[unknown — downgraded from typed fields]' WHERE ts IS NULL")

    op.alter_column('trace', 'ts', existing_type=sa.VARCHAR(), nullable=False)
    op.alter_column('trace', 'duration', existing_type=sa.VARCHAR(), nullable=False)
    op.alter_column('run', 'ts', existing_type=sa.VARCHAR(), nullable=False)
    op.alter_column('run', 'cost', existing_type=sa.VARCHAR(), nullable=False)
    op.alter_column('run', 'duration', existing_type=sa.VARCHAR(), nullable=False)
