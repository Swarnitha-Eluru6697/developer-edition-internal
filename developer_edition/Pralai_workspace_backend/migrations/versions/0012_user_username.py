"""users.username: the gateway username is an attribute of a user, no longer the key (W-020, X-003).

`users.external_id` used to hold the gateway's `x-user-name` (the token's preferred_username), while the runners keep the token's
`sub`; a rename of the person changed which row — and so which workspaces, Git credentials and sessions — was theirs.
`external_id` now holds the immutable control-plane subject (`x-user-id`, the token's `sub`). Rows created before this hold the
username there; the first request that carries both the subject and the username re-keys the matching row in place
(app.services.workspace_service.WorkspaceService.ensure_user), so nothing is orphaned and ownership follows the person.

`username` stores that display attribute (it is also what the catalog's policy engine knows the person by). Nullable and
additive: it is filled on the user's next request.

Revision ID: 0012_user_username
Revises: 0011_execution_cancel_lease
Create Date: 2026-10-14
"""
from __future__ import annotations
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op

revision: str = "0012_user_username"
down_revision: Union[str, None] = "0011_execution_cancel_lease"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("username", sa.String(255), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "username")
