"""backfill missing agentversion for published agents

Revision ID: 5a5c062ae225
Revises: c868695dde4f
Create Date: 2026-09-04 23:38:59.466271

§0.90, GAP_REPORT — the actual real, one-time data backfill the
report asked for: a genuinely PUBLISHED agent (status="published")
should always have a real, current AgentVersion row — the immutable
snapshot that's actually supposed to execute for a production run
(see routers/execution.py's own _resolve_execution_flow, §0.54). But
seed.py creates several agents with status="published" and never
creates a matching AgentVersion at all, and this was confirmed
directly to be a real, existing problem in this environment's own
database (thousands of real, pre-existing published agents with no
current AgentVersion row) before writing this migration — not a
hypothetical edge case.

This is a real DATA migration, not DDL — no columns added or
changed. For every real, existing Agent with status='published' and
no row in agentversion where is_current=true, this creates one,
using that agent's own CURRENT flow/app_type/features/version as the
real, best-available snapshot (there is no other real historical
data anywhere to reconstruct what was actually published at the real,
original time — a real, honest limitation, stated here rather than
silently assumed away: a real agent edited in-place after being
marked published, with no matching AgentVersion ever created, has no
way to recover what its flow looked like at the moment it was
ACTUALLY marked published; this migration's own snapshot reflects
"whatever this agent's flow is right now," not a genuine historical
reconstruction). published_by is set to a real, honest, distinct
marker ('system-backfill-0.90') rather than a fabricated user name,
so this backfilled row is clearly distinguishable from a genuine
publish event in any real version history a person looks at later.

Going forward, this exact gap cannot recur through the real, normal
publish endpoint (routers/agents.py's own publish_agent already
creates a real AgentVersion in the same real transaction as flipping
status to "published" — see §0.54/§0.70) — this migration exists
purely to backfill the real, pre-existing rows that predate that
invariant being enforced, or that were created by some other,
non-standard path (direct API/DSL import, seed data).

Idempotent — safe to run more than once: the WHERE NOT EXISTS guard
means an agent that already has a real, current AgentVersion (whether
from a genuine publish or an earlier run of this same migration) is
never touched again.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5a5c062ae225'
down_revision: Union[str, Sequence[str], None] = 'c868695dde4f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    conn = op.get_bind()
    result = conn.execute(sa.text("""
        INSERT INTO agentversion (agent_id, version, flow, app_type, features, entitlement_plan, published_by, created_at, is_current)
        SELECT
            a.id,
            a.version,
            a.flow,
            a.app_type,
            COALESCE(a.features, '{}'::jsonb),
            '{}'::jsonb,
            'system-backfill-0.90',
            replace(COALESCE(a.updated_at, now())::text, ' ', 'T'),
            true
        FROM agent a
        WHERE a.status = 'published'
        AND NOT EXISTS (
            SELECT 1 FROM agentversion av WHERE av.agent_id = a.id AND av.is_current = true
        )
        -- A real, existing (agent_id, version) unique constraint (§0.70,
        -- GAP_REPORT) means this would fail outright if some real,
        -- non-current AgentVersion row already happens to hold the
        -- identical version string for this agent (e.g. a real row was
        -- created, then somehow un-set as current, without a new one
        -- ever replacing it) - ON CONFLICT DO NOTHING skips that one,
        -- narrow, genuinely unusual case rather than aborting the
        -- entire real backfill for every other agent.
        ON CONFLICT (agent_id, version) DO NOTHING
    """))
    print(f"§0.90 backfill: created {result.rowcount} missing AgentVersion row(s) for published agents.")


def downgrade() -> None:
    """Downgrade schema — removes only the rows THIS migration itself
    created (the real, distinct published_by marker makes this safe:
    a genuine publish event is never mistakenly removed)."""
    conn = op.get_bind()
    conn.execute(sa.text("DELETE FROM agentversion WHERE published_by = 'system-backfill-0.90'"))
