"""group chat messages into conversations

ChatMessage was a flat list per (agent_id, invoking_user). That was
enough to hand prior turns to the model, but it made "conversations"
something the product could not express: starting a new one had to mean
DELETING the old, because there was nothing to distinguish them, and so
there was never any history left to look back at.

conversation_id gives each thread an identity. Starting a new
conversation now begins a new id instead of destroying the previous one,
and past threads can be listed and reopened.

Existing rows are backfilled into one conversation per
(agent_id, invoking_user) — which is exactly what they were: a single
running thread. Inventing boundaries inside them from timestamp gaps
would be a guess presented as history.

Revision ID: f1c8e3a90d42
Revises: e9a1c47b2f58
"""
import sqlalchemy as sa
from alembic import op

revision = "f1c8e3a90d42"
down_revision = "e9a1c47b2f58"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chatmessage", sa.Column("conversation_id", sa.String(), nullable=True))
    # Every read is "this conversation's messages, in order", and every
    # listing is "this user's conversations for this agent".
    op.create_index("ix_chatmessage_conversation_id", "chatmessage", ["conversation_id"])

    # One conversation per existing (agent, user) pair. Deterministic
    # rather than random so re-running against a partially migrated
    # database cannot split one thread across two ids.
    op.execute(
        "UPDATE chatmessage "
        "SET conversation_id = 'conv-' || md5(agent_id || ':' || invoking_user) "
        "WHERE conversation_id IS NULL"
    )


def downgrade() -> None:
    op.drop_index("ix_chatmessage_conversation_id", table_name="chatmessage")
    op.drop_column("chatmessage", "conversation_id")
