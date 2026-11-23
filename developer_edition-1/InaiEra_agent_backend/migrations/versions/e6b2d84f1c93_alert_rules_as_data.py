"""alert rules as data — configurable per agent

The rules were module-level Python functions with env-var thresholds:
tuning one meant editing code and redeploying, and there was no way to
hold one agent to a tighter bar than the rest. As rows they can be
added, tuned and scoped per agent from the UI.

The seven default rows reproduce exactly what the hardcoded rules did,
so upgrading changes no behaviour — only where the numbers live.

Revision ID: e6b2d84f1c93
Revises: d5a1c73e8b40
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "e6b2d84f1c93"
down_revision = "d5a1c73e8b40"
branch_labels = None
depends_on = None

KINDS = ("error_rate", "denied_burst", "guardrail_block", "step_limit",
         "latency_spike", "cost_spike", "quality_breach")

# name, kind, severity, threshold, window_seconds, min_runs
DEFAULTS = [
    ("error-rate",      "error_rate",      "high",   25, 86400, 20),
    ("denied-burst",    "denied_burst",    "high",    3, 86400,  1),
    ("guardrail-block", "guardrail_block", "medium",  3, 86400,  1),
    ("step-limit",      "step_limit",      "medium",  1, 86400,  1),
    ("latency-spike",   "latency_spike",   "medium",  3, 86400, 20),
    ("cost-spike",      "cost_spike",      "medium",  3, 86400,  1),
    ("quality-breach",  "quality_breach",  "high",   70,     0,  1),
]


def upgrade() -> None:
    op.create_table(
        "alertrule",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("severity", sa.String(), nullable=False, server_default="medium"),
        sa.Column("threshold", sa.Float(), nullable=False, server_default="1"),
        sa.Column("window_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("min_runs", sa.Integer(), nullable=False, server_default="20"),
        sa.Column("fail_classes", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("agent_id", sa.String(), sa.ForeignKey("agent.id"), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_unique_constraint("uq_alertrule_name", "alertrule", ["name"])
    op.create_index("ix_alertrule_agent_id", "alertrule", ["agent_id"])
    op.create_check_constraint(
        "ck_alertrule_kind", "alertrule",
        "kind IN (" + ",".join(f"'{k}'" for k in KINDS) + ")")
    op.create_check_constraint(
        "ck_alertrule_severity", "alertrule", "severity IN ('low','medium','high')")
    op.create_check_constraint(
        "ck_alertrule_threshold", "alertrule", "threshold >= 0")

    # Seed the defaults so an upgrade changes where the numbers live, not
    # what the platform alerts on. Without this the sweep would evaluate
    # an empty rule set and silently stop alerting.
    rules = sa.table(
        "alertrule",
        sa.column("name", sa.String), sa.column("kind", sa.String),
        sa.column("severity", sa.String), sa.column("threshold", sa.Float),
        sa.column("window_seconds", sa.Integer), sa.column("min_runs", sa.Integer),
    )
    op.bulk_insert(rules, [
        {"name": n, "kind": k, "severity": s, "threshold": t,
         "window_seconds": w, "min_runs": m}
        for n, k, s, t, w, m in DEFAULTS
    ])


def downgrade() -> None:
    op.drop_index("ix_alertrule_agent_id", table_name="alertrule")
    op.drop_table("alertrule")
