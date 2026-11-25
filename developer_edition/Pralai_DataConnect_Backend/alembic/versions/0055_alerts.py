"""Alerts: Slack / Teams channels (with their webhook URL encrypted), rules, what the sweep saw, alerts with their history, sweep state

A failed / degraded pipeline, a failed deploy, an error-threshold pause, dead-letter errors or failing quality checks raise an alert by rule and
post it to the organization's Slack or Microsoft Teams channels (app/services/alerts/, docs/ALERTS_IMPLEMENTATION_GUIDE.md). Ported from the
Workspace's 0015_alerts; per organization, watching pipelines.

Revision ID: 0055_alerts
Revises: 0054_transform_policy_origin
Create Date: 2026-10-30
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0055_alerts"
down_revision = "0054_transform_policy_origin"
branch_labels = None
depends_on = None


def _id() -> sa.Column:
    return sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True)


def _org() -> sa.Column:
    return sa.Column("org_id", sa.String(length=100), sa.ForeignKey("dc_organizations.org_id"), nullable=False)


def _stamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "dc_alert_channels", _id(), _org(),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("url_hint", sa.String(length=200), nullable=False),
        sa.Column("min_severity", sa.String(length=10), nullable=False),
        sa.Column("scope_pipeline_id", sa.String(length=64), nullable=True),
        sa.Column("scope_pipeline_name", sa.String(length=255), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_ok", sa.Boolean(), nullable=True),
        sa.Column("last_detail", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=255), nullable=True),
        *_stamps(),
        sa.CheckConstraint("kind IN ('slack','teams')", name="ck_dc_alert_channels_kind"),
        sa.CheckConstraint("min_severity IN ('low','medium','high')", name="ck_dc_alert_channels_severity"),
    )
    op.create_index("ix_dc_alert_channels_org_id", "dc_alert_channels", ["org_id"])
    op.create_index("uq_dc_alert_channels_org_name", "dc_alert_channels", ["org_id", sa.text("lower(name)")], unique=True)

    op.create_table(
        "dc_alert_channel_secrets",
        sa.Column("channel_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("dc_alert_channels.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("envelope", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    )

    op.create_table(
        "dc_alert_rules", _id(), _org(),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("severity", sa.String(length=10), nullable=False),
        sa.Column("threshold", sa.Integer(), nullable=False),
        sa.Column("window_seconds", sa.Integer(), nullable=False),
        sa.Column("min_runs", sa.Integer(), nullable=False),
        sa.Column("scope_pipeline_id", sa.String(length=64), nullable=True),
        sa.Column("scope_pipeline_name", sa.String(length=255), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=True),
        *_stamps(),
        sa.CheckConstraint("severity IN ('low','medium','high')", name="ck_dc_alert_rules_severity"),
        sa.CheckConstraint("threshold > 0", name="ck_dc_alert_rules_threshold"),
        sa.CheckConstraint("min_runs >= 1", name="ck_dc_alert_rules_min_runs"),
    )
    op.create_index("ix_dc_alert_rules_org_id", "dc_alert_rules", ["org_id"])
    op.create_index("uq_dc_alert_rules_org_name", "dc_alert_rules", ["org_id", sa.text("lower(name)")], unique=True)

    op.create_table(
        "dc_alert_observations", _id(), _org(),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("ref_id", sa.String(length=300), nullable=False),
        sa.Column("pipeline_id", sa.String(length=64), nullable=False),
        sa.Column("pipeline_name", sa.String(length=255), nullable=False),
        sa.Column("direction", sa.String(length=10), nullable=False),
        sa.Column("category", sa.String(length=20), nullable=False),
        sa.Column("outcome", sa.String(length=30), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("org_id", "source", "ref_id", name="uq_dc_alert_observations_ref"),
    )
    op.create_index("ix_dc_alert_observations_pipeline", "dc_alert_observations", ["org_id", "pipeline_id", "occurred_at"])

    op.create_table(
        "dc_alerts", _id(), _org(),
        sa.Column("rule_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("dc_alert_rules.id", ondelete="SET NULL"), nullable=True),
        sa.Column("rule_name", sa.String(length=120), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("severity", sa.String(length=10), nullable=False),
        sa.Column("pipeline_id", sa.String(length=64), nullable=False),
        sa.Column("pipeline_name", sa.String(length=255), nullable=False),
        sa.Column("direction", sa.String(length=10), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.String(length=200), nullable=False),
        sa.Column("state", sa.String(length=15), nullable=False),
        sa.Column("delivery", sa.String(length=12), nullable=False),
        sa.Column("delivery_attempts", sa.Integer(), nullable=False),
        sa.Column("raised_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("evidence_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_ref_id", sa.String(length=300), nullable=True),
        *_stamps(),
        sa.CheckConstraint("severity IN ('low','medium','high')", name="ck_dc_alerts_severity"),
        sa.CheckConstraint("state IN ('open','acknowledged','resolved')", name="ck_dc_alerts_state"),
        sa.CheckConstraint("delivery IN ('pending','sent','no_channel','failed')", name="ck_dc_alerts_delivery"),
    )
    op.create_index("ix_dc_alerts_org_state", "dc_alerts", ["org_id", "state", "raised_at"])
    op.create_index("ix_dc_alerts_fingerprint", "dc_alerts", ["org_id", "fingerprint", "state"])
    # At most one live (open / acknowledged) alert per rule + pipeline: two sweeps racing cannot raise it twice.
    op.create_index("uq_dc_alerts_live_fingerprint", "dc_alerts", ["org_id", "fingerprint"], unique=True,
                    postgresql_where=sa.text("state IN ('open','acknowledged')"))

    op.create_table(
        "dc_alert_events", _id(),
        sa.Column("alert_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("dc_alerts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("action", sa.String(length=200), nullable=False),
        sa.Column("actor", sa.String(length=255), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_dc_alert_events_alert", "dc_alert_events", ["alert_id", "at"])

    op.create_table(
        "dc_alert_state",
        sa.Column("org_id", sa.String(length=100), sa.ForeignKey("dc_organizations.org_id"), primary_key=True),
        sa.Column("defaults_seeded", sa.Boolean(), nullable=False),
        sa.Column("leased_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sweep_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sweep_ok", sa.Boolean(), nullable=True),
        sa.Column("last_sweep_detail", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("dc_alert_state")
    op.drop_index("ix_dc_alert_events_alert", table_name="dc_alert_events")
    op.drop_table("dc_alert_events")
    op.drop_index("uq_dc_alerts_live_fingerprint", table_name="dc_alerts")
    op.drop_index("ix_dc_alerts_fingerprint", table_name="dc_alerts")
    op.drop_index("ix_dc_alerts_org_state", table_name="dc_alerts")
    op.drop_table("dc_alerts")
    op.drop_index("ix_dc_alert_observations_pipeline", table_name="dc_alert_observations")
    op.drop_table("dc_alert_observations")
    op.drop_index("uq_dc_alert_rules_org_name", table_name="dc_alert_rules")
    op.drop_index("ix_dc_alert_rules_org_id", table_name="dc_alert_rules")
    op.drop_table("dc_alert_rules")
    op.drop_table("dc_alert_channel_secrets")
    op.drop_index("uq_dc_alert_channels_org_name", table_name="dc_alert_channels")
    op.drop_index("ix_dc_alert_channels_org_id", table_name="dc_alert_channels")
    op.drop_table("dc_alert_channels")
