"""saved result provenance and the run->trace link

SavedResult accepted caller-supplied invoking_user, input, output and
structured_output, so a client could create a row indistinguishable from
a real execution of an agent by another user. And Agent-level listing
returned every user's rows while delete accepted any row id, so users of
the same agent were not isolated at all.

Run.trace_id exists because _finalize_run created both ids and handed
both to the client without relating them on the server — provenance for
a saved result needs that link to be authoritative rather than
client-asserted.

Revision ID: e9a1c47b2f58
Revises: d7f4a2b8c916
"""
import sqlalchemy as sa
from alembic import op

revision = "e9a1c47b2f58"
down_revision = "d7f4a2b8c916"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("run", sa.Column("trace_id", sa.String(), nullable=True))
    op.create_index("ix_run_trace_id", "run", ["trace_id"])

    # Existing rows were created when everything was caller-supplied, so
    # "manual" is the only honest value for them: nothing verified them
    # against a Run at the time. Marking them "execution" retroactively
    # would assert a provenance chain that never existed.
    op.add_column("savedresult", sa.Column("provenance", sa.String(), nullable=False,
                                            server_default="manual"))
    op.add_column("savedresult", sa.Column("run_id", sa.String(), nullable=True))
    op.add_column("savedresult", sa.Column("trace_id", sa.String(), nullable=True))
    op.add_column("savedresult", sa.Column("agent_version", sa.String(), nullable=True))
    op.create_index("ix_savedresult_provenance", "savedresult", ["provenance"])
    op.create_index("ix_savedresult_run_id", "savedresult", ["run_id"])
    # The query every owner-scoped list makes.
    op.create_index("ix_savedresult_agent_owner", "savedresult", ["agent_id", "invoking_user"])


def downgrade() -> None:
    op.drop_index("ix_savedresult_agent_owner", table_name="savedresult")
    op.drop_index("ix_savedresult_run_id", table_name="savedresult")
    op.drop_index("ix_savedresult_provenance", table_name="savedresult")
    op.drop_column("savedresult", "agent_version")
    op.drop_column("savedresult", "trace_id")
    op.drop_column("savedresult", "run_id")
    op.drop_column("savedresult", "provenance")
    op.drop_index("ix_run_trace_id", table_name="run")
    op.drop_column("run", "trace_id")
