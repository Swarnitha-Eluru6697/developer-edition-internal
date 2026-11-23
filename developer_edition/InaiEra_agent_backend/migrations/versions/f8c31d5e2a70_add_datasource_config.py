"""add config to datasource

Revision ID: f8c31d5e2a70
Revises: e5f27a1b9c34
Create Date: 2026-09-09 15:10:00.000000

The Integrations page collected client_id/client_secret/tenant_id for
the OAuth-style sources and a credentials blob for the API-key ones,
then stored none of it — both configure endpoints only set
status='connected'. A source could therefore read as connected while
no code could authenticate to it, which is why nothing could browse
folders or ingest from one.

This column is where those details now live, encrypted per
DS_SENSITIVE_KEYS and masked on read.

Scoped to this one column, matching the precedent of every prior
migration here.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'f8c31d5e2a70'
down_revision: Union[str, Sequence[str], None] = 'e5f27a1b9c34'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('datasource',
                  sa.Column('config', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    # Existing rows claiming "connected" were never backed by a stored
    # credential, so that status was not true. Reset them rather than
    # leaving a source that browsing will fail on for no visible reason.
    op.execute("UPDATE datasource SET config = '{}'::jsonb WHERE config IS NULL")
    op.execute("UPDATE datasource SET status = 'needs_auth' WHERE status = 'connected'")


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('datasource', 'config')
