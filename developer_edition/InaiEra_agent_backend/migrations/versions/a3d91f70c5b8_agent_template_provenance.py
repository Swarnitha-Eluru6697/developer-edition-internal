"""record which template an agent was created from

`template_name` was accepted by POST /api/agents, used to deep-copy the
template's flow, and then discarded. Nothing recorded where an agent
came from, so Monitoring's "Marketplace reuse" card had no data to
compute from and shipped as a hardcoded dash citing §0.77.

Existing rows stay NULL rather than being guessed at: an agent's flow
may match a template today because it was copied from one, or because
someone built the same thing, and there is no way to tell those apart
after the fact. Only agents created from here on carry provenance,
which is why the metric is reported as a count of template-derived
agents rather than a percentage of all agents.

Revision ID: a3d91f70c5b8
Revises: f1c8e3a90d42
"""
import sqlalchemy as sa
from alembic import op

revision = "a3d91f70c5b8"
down_revision = "f1c8e3a90d42"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent", sa.Column("template_name", sa.String(), nullable=True))
    # The metric counts non-NULL rows, and the Templates page will want
    # "agents from this template" next.
    op.create_index("ix_agent_template_name", "agent", ["template_name"])


def downgrade() -> None:
    op.drop_index("ix_agent_template_name", table_name="agent")
    op.drop_column("agent", "template_name")
