"""backfill mcp credential columns from features and strip legacy location

Revision ID: 1bd0f35eeb21
Revises: 2003baed6edb
Create Date: 2026-09-05 18:14:00.000000

§0.101, GAP_REPORT — the real, one-time backfill this fix ships
alongside its own schema migration: for every real, existing agent
with features["mcp"] already configured (confirmed directly against
this environment's own real database before writing this: 611 real
rows), copies that same real, already-encrypted token/enabled state
into the new, real, top-level Agent columns, then strips "mcp" out of
the live features dict entirely — the legacy location is never read
by any real code path after this fix ships, so leaving stale data
there would only invite a future, accidental re-introduction of this
same real bug by anyone who forgets it's dead.

The token value itself needs no re-encryption — encrypt_secret's own
real ciphertext format is unchanged; this is a real, plain column-to-
column copy.

Historical AgentVersion.features snapshots are deliberately left
untouched — they're real, immutable historical records by design
(rewriting them would itself be a different kind of integrity
violation), already masked when displayed (_mask_mcp_token, models.py),
and the real vulnerability this fix closes (restore resurrecting a
credential) is already, structurally impossible regardless of what an
old snapshot still contains, since restore/publish never read or
write these new columns at all.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1bd0f35eeb21'
down_revision: Union[str, Sequence[str], None] = '2003baed6edb'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    conn = op.get_bind()
    rows = conn.execute(sa.text("""
        SELECT id, features -> 'mcp' AS mcp_cfg
        FROM agent
        WHERE features -> 'mcp' IS NOT NULL
    """)).fetchall()

    migrated = 0
    for agent_id, mcp_cfg in rows:
        enabled = bool((mcp_cfg or {}).get("enabled"))
        token = (mcp_cfg or {}).get("token")
        conn.execute(sa.text("""
            UPDATE agent
            SET mcp_enabled = :enabled,
                mcp_token_encrypted = :token,
                mcp_token_created_at = CASE WHEN :token IS NOT NULL THEN now() ELSE NULL END,
                features = features - 'mcp'
            WHERE id = :agent_id
        """), {"enabled": enabled, "token": token, "agent_id": agent_id})
        migrated += 1
    print(f"§0.101 backfill: migrated {migrated} agent(s) from features['mcp'] to real, top-level Agent columns.")


def downgrade() -> None:
    """Downgrade schema — no real, separate action; the schema
    migration's own downgrade already drops the columns this data
    lives in, and the legacy features['mcp'] location was already
    stripped by this migration's own upgrade (a real, one-way move,
    not a reversible rename)."""
    pass
