"""Store the caller's groups and permissions (not role names) for deferred UCS checks (contract 01-CONTRACT.md).

A queued execution and a registered SQL job are re-checked against UCS later, with no signed-in user to ask. They used to keep
the caller's role names, which UCS no longer reads; they now keep the caller's Keycloak groups (UCS's data-level grants) and
permissions. Old values were role names, not groups, so they are not carried over: a pending execution or an existing SQL job
is re-checked with no groups until it is submitted / registered again.

Revision ID: 0015_identity_groups_perms
Revises: 0014_job_connection_bindings
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0015_identity_groups_perms"
down_revision: Union[str, None] = "0014_job_connection_bindings"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("executions", sa.Column("principal_groups", sa.Text(), nullable=True))
    op.add_column("executions", sa.Column("principal_permissions", sa.Text(), nullable=True))
    op.drop_column("executions", "principal_roles")
    op.add_column("job_connection_bindings", sa.Column("groups", sa.Text(), nullable=False, server_default="[]"))
    op.add_column("job_connection_bindings", sa.Column("permissions", sa.Text(), nullable=False, server_default="[]"))
    op.drop_column("job_connection_bindings", "roles")


def downgrade() -> None:
    op.add_column("job_connection_bindings", sa.Column("roles", sa.Text(), nullable=False, server_default=""))
    op.drop_column("job_connection_bindings", "permissions")
    op.drop_column("job_connection_bindings", "groups")
    op.add_column("executions", sa.Column("principal_roles", sa.String(1000), nullable=True))
    op.drop_column("executions", "principal_permissions")
    op.drop_column("executions", "principal_groups")
