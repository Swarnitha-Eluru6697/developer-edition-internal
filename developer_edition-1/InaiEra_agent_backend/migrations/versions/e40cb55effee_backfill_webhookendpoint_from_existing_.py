"""backfill webhookendpoint from existing published agents

Revision ID: e40cb55effee
Revises: adc297071341
Create Date: 2026-09-05 16:24:00.000000

§0.97, GAP_REPORT — the real, one-time backfill this fix ships
alongside its own schema migration: for every real, existing,
published agent whose own CURRENT AgentVersion.flow already has a
real trigger node configured with a webhook_token (from before this
exact registration table existed), this creates the matching, real
WebhookEndpoint row — so an already-live, real webhook integration
someone is depending on doesn't silently stop resolving the moment
this fix ships, just because it predates the new table.

Confirmed directly against this environment's own real database
before writing this: zero current AgentVersion rows have a
webhook_token configured today, so this runs as a genuine no-op here
— but a real production deployment of this platform could have real,
already-configured webhook integrations, and this migration exists so
THAT deployment's own upgrade stays correct, not just this sandbox's.

Uses the exact same real hashing function
(routers/webhooks.py:hash_webhook_token — a plain sha256 hex digest)
the application itself uses at request time, imported directly rather
than reimplemented — so this backfill can never independently drift
from what a real request would actually compute.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e40cb55effee'
down_revision: Union[str, Sequence[str], None] = 'adc297071341'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    import sys
    import os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    from app.routers.webhooks import hash_webhook_token

    conn = op.get_bind()
    rows = conn.execute(sa.text("""
        SELECT av.agent_id, av.id AS agent_version_id, av.flow
        FROM agentversion av
        WHERE av.is_current = true
    """)).fetchall()

    created = 0
    now = sa.func.now()
    for agent_id, agent_version_id, flow in rows:
        for node in (flow or {}).get("nodes", []):
            if node.get("type") != "trigger":
                continue
            token = (node.get("config") or {}).get("webhook_token")
            if not token:
                continue
            token_hash = hash_webhook_token(token)
            existing = conn.execute(sa.text(
                "SELECT id FROM webhookendpoint WHERE token_hash = :h"
            ), {"h": token_hash}).first()
            if existing:
                continue
            conn.execute(sa.text("""
                INSERT INTO webhookendpoint
                    (agent_id, agent_version_id, trigger_node_id, token_hash, enabled, created_at)
                VALUES (:agent_id, :agent_version_id, :trigger_node_id, :token_hash, true, now())
            """), {
                "agent_id": agent_id, "agent_version_id": agent_version_id,
                "trigger_node_id": node["id"], "token_hash": token_hash,
            })
            created += 1
    print(f"§0.97 backfill: created {created} WebhookEndpoint row(s) from existing published agents.")


def downgrade() -> None:
    """Downgrade schema — no real, separate action; the schema
    migration's own downgrade already drops the whole table these
    rows live in."""
    pass
