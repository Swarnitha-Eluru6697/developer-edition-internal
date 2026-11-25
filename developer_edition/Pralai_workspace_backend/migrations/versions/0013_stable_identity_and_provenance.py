"""A connection's governed identity is stored, and a Git import records what it asked for (W-011, W-014).

  - `database_connections.ucs_resource_fqn`: the canonical resource UCS is asked about when the connection is used. It used to be derived on
    every call from the connection's `catalog_name`, else its mutable `name`, so a rename or a name collision moved the policy identity of the
    same physical connection. Now it is persisted: the UCS catalog the connection was linked to (verified to exist in UCS when linked), else
    `<catalog>.connection.<connection id>` (the row's immutable id). Backfill: the existing link, else the id form. A rename never changes it.
  - `git_bindings.requested_ref`: the branch or tag the user explicitly asked to import, NULL when they asked for the repository's default.
    Together with the existing `last_synced_sha` (now the commit the import really checked out) a binding records both what was requested
    and what was actually imported; a requested ref that cannot be cloned now fails instead of falling back to the default branch.

Additive and nullable. The default catalog for the id form is DATA_POLICY_CATALOG (default "inaiera_workspace") at migration time.

Revision ID: 0013_stable_identity_prov
Revises: 0012_user_username
Create Date: 2026-10-14
"""
from __future__ import annotations

import os
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0013_stable_identity_prov"
down_revision: Union[str, None] = "0012_user_username"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("database_connections", sa.Column("ucs_resource_fqn", sa.String(500), nullable=True))
    catalog = os.environ.get("DATA_POLICY_CATALOG", "inaiera_workspace")
    op.execute(sa.text(
        "UPDATE database_connections SET ucs_resource_fqn = COALESCE(NULLIF(catalog_name, ''), :prefix || id::text)"
    ).bindparams(prefix=f"{catalog}.connection."))
    op.add_column("git_bindings", sa.Column("requested_ref", sa.String(255), nullable=True))


def downgrade() -> None:
    op.drop_column("git_bindings", "requested_ref")
    op.drop_column("database_connections", "ucs_resource_fqn")
