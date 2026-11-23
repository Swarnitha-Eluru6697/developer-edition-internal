"""Quality outbox: short claim transactions and persisted retry backoff (FL-004, FL-005).

  - `lease_owner`, `lease_expires_at` and the new status `processing`: a worker claims a batch in a short transaction,
    delivers to UCS with no row lock or transaction held, then finalizes each row in another one. A claim whose lease
    lapsed (worker died) is claimable again.
  - `next_attempt_at`: the earliest time a failed row may be tried again (exponential backoff, persisted).
  - the new status `parked`: the row failed `QUALITY_OUTBOX_MAX_ATTEMPTS` times; it is kept, not retried.

ADDITIVE ONLY: existing rows keep their status, get next_attempt_at = now() (so pending ones are due at once).

Revision ID: 0009_quality_outbox_backoff
Revises:     0008_execution_manifest
"""
from __future__ import annotations
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op

revision: str = "0009_quality_outbox_backoff"
down_revision: Union[str, None] = "0008_execution_manifest"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("quality_result_outbox", sa.Column("lease_owner", sa.String(128)))
    op.add_column("quality_result_outbox", sa.Column("lease_expires_at", sa.DateTime(timezone=True)))
    op.add_column("quality_result_outbox", sa.Column("next_attempt_at", sa.DateTime(timezone=True),
                                                     server_default=sa.func.now(), nullable=False))
    op.drop_constraint("ck_quality_outbox_status", "quality_result_outbox", type_="check")
    op.create_check_constraint(
        "ck_quality_outbox_status", "quality_result_outbox",
        "status IN ('pending', 'processing', 'delivered', 'rejected', 'skipped', 'parked')")
    op.create_index("ix_quality_outbox_due", "quality_result_outbox", ["status", "next_attempt_at"])


def downgrade() -> None:
    op.execute("UPDATE quality_result_outbox SET status = 'pending' WHERE status IN ('processing', 'parked')")
    op.drop_index("ix_quality_outbox_due", table_name="quality_result_outbox")
    op.drop_constraint("ck_quality_outbox_status", "quality_result_outbox", type_="check")
    op.create_check_constraint(
        "ck_quality_outbox_status", "quality_result_outbox",
        "status IN ('pending', 'delivered', 'rejected', 'skipped')")
    op.drop_column("quality_result_outbox", "next_attempt_at")
    op.drop_column("quality_result_outbox", "lease_expires_at")
    op.drop_column("quality_result_outbox", "lease_owner")
