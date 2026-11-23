"""rename stored agent roles to UCS's agent privileges

Agent roles were five product-only names (agent_viewer … agent_admin)
that UCS could not show: it grants an agent only USE_AGENT,
CREATE_AGENT_VERSION or CREATE_AGENT. The product now offers exactly
those three (app/agent_roles.py), so stored bindings are renamed to the
role their UCS level already granted — nobody's access changes, the
binding just reads back under the name UCS shows.

Revision ID: b7d2e4f9a013
Revises: a1b2c3d4e5f6
"""
from alembic import op

revision = "b7d2e4f9a013"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None

_RENAME = {
    "agent_viewer": "use_agent", "agent_user": "use_agent",
    "agent_developer": "create_agent_version", "agent_manager": "create_agent_version",
    "agent_admin": "create_agent",
}
_BY_TIER = {"select": "use_agent", "writer": "create_agent_version", "owner": "create_agent"}


def upgrade() -> None:
    for old, new in _RENAME.items():
        op.execute(f"UPDATE role SET agent_role = '{new}' WHERE agent_role = '{old}'")
    for tier, new in _BY_TIER.items():
        op.execute(f"UPDATE role SET agent_role = '{new}' WHERE agent_role IS NULL AND tier = '{tier}'")


def downgrade() -> None:
    # The five-role names cannot be recovered (two collapsed onto each of
    # two levels); map back to the nearest one each level used to default to.
    back = {"use_agent": "agent_user", "create_agent_version": "agent_developer",
            "create_agent": "agent_admin"}
    for new, old in back.items():
        op.execute(f"UPDATE role SET agent_role = '{old}' WHERE agent_role = '{new}'")
