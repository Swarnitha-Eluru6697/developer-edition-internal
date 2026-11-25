"""record an agent role alongside the UCS tier on each grant

A grant carried only `tier` — select/writer/owner — which is UCS's data
vocabulary and cannot express the difference between "may run this
agent" and "may publish a new version of it". The five agent roles live
in app/agent_roles.py; this stores which one a group was given.

The tier is kept, not replaced: it is what UCS is actually told, and
several roles map onto the same tier, so it cannot be derived back.

Existing rows are backfilled to the nearest role so a grant keeps
meaning what it already meant — select->agent_user,
writer->agent_developer, owner->agent_admin.

Revision ID: b7e2c04af913
Revises: a3d91f70c5b8
"""
import sqlalchemy as sa
from alembic import op

revision = "b7e2c04af913"
down_revision = "a3d91f70c5b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("role", sa.Column("agent_role", sa.String(), nullable=True))
    op.create_index("ix_role_agent_role", "role", ["agent_role"])
    op.execute(
        "UPDATE role SET agent_role = CASE tier "
        "  WHEN 'select' THEN 'agent_user' "
        "  WHEN 'writer' THEN 'agent_developer' "
        "  WHEN 'owner'  THEN 'agent_admin' "
        "  ELSE 'agent_user' END "
        "WHERE agent_role IS NULL"
    )


def downgrade() -> None:
    op.drop_index("ix_role_agent_role", table_name="role")
    op.drop_column("role", "agent_role")
