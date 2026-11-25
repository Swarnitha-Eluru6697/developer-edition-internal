"""add agent_version_id agent_version flow_hash to run and trace, run_id to hitlrequest

Revision ID: efca80a4fdb0
Revises: 5a5c062ae225
Create Date: 2026-09-05 00:11:29.008647

§0.91, GAP_REPORT — the actual fix for a real, reported gap: neither
Run nor Trace ever recorded which real AgentVersion snapshot (or,
for a draft/preview run, which real live-flow content) actually
executed at invocation time — only Trace.version existed, and that
was set from agent.version at FINALIZATION time, not from whatever
version was actually resolved and executed when the run began. For a
real, long-running durable workflow, those two can genuinely differ:
a workflow that started on a real, immutable v5 snapshot could have
the agent published to v6 before the workflow finishes and this row
gets finalized — the eventual Trace would incorrectly claim v6,
despite the real, frozen v5 nodes being what actually ran the whole
time.

Adds agent_version_id (real FK to agentversion.id), agent_version (the
real, human-readable version string actually resolved), and flow_hash
(a real sha256 digest of the exact flow JSON actually executed) to
both Run and Trace. Also adds run_id to HitlRequest (real FK to
run.id) — the second real half of this same fix: run_agent now
creates a real Run row at real INVOCATION time, before execution even
starts, so a workflow that pauses for human input has a real, visible
Run row for the entire real duration of the pause, not just once it
eventually reaches a terminal state; run_id is what lets
respond_to_hitl find and update that same real row across the full
real pause/resume lifecycle.

This migration is intentionally scoped to ONLY the columns/FKs this
fix actually needs — `alembic revision --autogenerate` also detected
several real, PRE-EXISTING, unrelated schema-drift items (a TEXT() vs.
AutoString() cosmetic type difference on several unrelated columns, a
nullable-constraint difference on agent.input_variables/
hitlrequest.resolved_result, and a dropped partial index on
agentversion) that have nothing to do with this fix and were
deliberately left out of this migration rather than bundled in
alongside genuinely unrelated changes.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel

# revision identifiers, used by Alembic.
revision: str = 'efca80a4fdb0'
down_revision: Union[str, Sequence[str], None] = '5a5c062ae225'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('run', sa.Column('agent_version_id', sa.Integer(), nullable=True))
    op.add_column('run', sa.Column('agent_version', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.add_column('run', sa.Column('flow_hash', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.create_foreign_key('fk_run_agent_version_id_agentversion', 'run', 'agentversion', ['agent_version_id'], ['id'])

    op.add_column('trace', sa.Column('agent_version_id', sa.Integer(), nullable=True))
    op.add_column('trace', sa.Column('agent_version', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.add_column('trace', sa.Column('flow_hash', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.create_foreign_key('fk_trace_agent_version_id_agentversion', 'trace', 'agentversion', ['agent_version_id'], ['id'])

    op.add_column('hitlrequest', sa.Column('run_id', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.create_foreign_key('fk_hitlrequest_run_id_run', 'hitlrequest', 'run', ['run_id'], ['id'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('fk_hitlrequest_run_id_run', 'hitlrequest', type_='foreignkey')
    op.drop_column('hitlrequest', 'run_id')

    op.drop_constraint('fk_trace_agent_version_id_agentversion', 'trace', type_='foreignkey')
    op.drop_column('trace', 'flow_hash')
    op.drop_column('trace', 'agent_version')
    op.drop_column('trace', 'agent_version_id')

    op.drop_constraint('fk_run_agent_version_id_agentversion', 'run', type_='foreignkey')
    op.drop_column('run', 'flow_hash')
    op.drop_column('run', 'agent_version')
    op.drop_column('run', 'agent_version_id')
