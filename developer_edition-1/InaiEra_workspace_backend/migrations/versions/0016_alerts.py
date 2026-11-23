"""Alerts: channels (Slack / Teams webhooks), rules, observations, alerts with their history, and the sweep state.

See app.models (AlertChannel, AlertRule, AlertObservation, Alert, AlertEvent, AlertState). A channel's webhook URL is never stored in
the database (secrets backend; only `secret_ref` and a display hint).

Revision ID: 0016_alerts
Revises: 0015_identity_groups_perms
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0016_alerts"
down_revision: Union[str, None] = "0015_identity_groups_perms"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _id() -> sa.Column:
    return sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"))


def _ts(name: str, **kw) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), **kw)


def upgrade() -> None:
    op.create_table(
        "alert_channels", _id(),
        sa.Column("org_id", sa.String(255), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("secret_ref", sa.String(300), nullable=False),
        sa.Column("url_hint", sa.String(200), nullable=False, server_default=""),
        sa.Column("min_severity", sa.String(10), nullable=False, server_default="low"),
        sa.Column("scope_job_key", sa.String(100)),
        sa.Column("scope_job_name", sa.String(300)),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        _ts("last_attempt_at"),
        sa.Column("last_ok", sa.Boolean()),
        sa.Column("last_detail", sa.Text()),
        sa.Column("created_by", sa.String(255)),
        _ts("created_at", server_default=sa.func.now(), nullable=False),
        _ts("updated_at", server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("org_id", "name", name="uq_alert_channel_org_name"),
        sa.CheckConstraint("kind IN ('slack','teams')", name="ck_alert_channel_kind"),
        sa.CheckConstraint("min_severity IN ('low','medium','high')", name="ck_alert_channel_severity"),
    )
    op.create_table(
        "alert_rules", _id(),
        sa.Column("org_id", sa.String(255), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("severity", sa.String(10), nullable=False, server_default="medium"),
        sa.Column("threshold", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("window_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("min_runs", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("scope_job_key", sa.String(100)),
        sa.Column("scope_job_name", sa.String(300)),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.String(255)),
        _ts("created_at", server_default=sa.func.now(), nullable=False),
        _ts("updated_at", server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("org_id", "name", name="uq_alert_rule_org_name"),
        sa.CheckConstraint("severity IN ('low','medium','high')", name="ck_alert_rule_severity"),
        sa.CheckConstraint("threshold > 0", name="ck_alert_rule_threshold"),
        sa.CheckConstraint("min_runs >= 1", name="ck_alert_rule_min_runs"),
    )
    op.create_table(
        "alert_observations", _id(),
        sa.Column("org_id", sa.String(255), nullable=False),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("ref_id", sa.String(200), nullable=False),
        sa.Column("job_key", sa.String(100), nullable=False),
        sa.Column("job_name", sa.String(300), nullable=False),
        sa.Column("job_kind", sa.String(20), nullable=False),
        sa.Column("category", sa.String(20), nullable=False),
        sa.Column("outcome", sa.String(20), nullable=False),
        sa.Column("failure_class", sa.String(20)),
        sa.Column("detail", sa.Text()),
        _ts("occurred_at", nullable=False),
        _ts("created_at", server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("org_id", "source", "ref_id", name="uq_alert_observation_ref"),
    )
    op.create_index("ix_alert_observation_job", "alert_observations", ["org_id", "job_key", "occurred_at"])
    op.create_table(
        "alerts", _id(),
        sa.Column("org_id", sa.String(255), nullable=False),
        sa.Column("rule_id", UUID(as_uuid=True), sa.ForeignKey("alert_rules.id", ondelete="SET NULL")),
        sa.Column("rule_name", sa.String(120), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("severity", sa.String(10), nullable=False),
        sa.Column("job_key", sa.String(100), nullable=False),
        sa.Column("job_name", sa.String(300), nullable=False),
        sa.Column("job_kind", sa.String(20), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.String(200), nullable=False),
        sa.Column("state", sa.String(15), nullable=False, server_default="open"),
        sa.Column("delivery", sa.String(12), nullable=False, server_default="pending"),
        sa.Column("delivery_attempts", sa.Integer(), nullable=False, server_default="0"),
        _ts("raised_at", nullable=False),
        _ts("sent_at"),
        _ts("resolved_at"),
        sa.Column("last_run_id", sa.String(200)),
        _ts("created_at", server_default=sa.func.now(), nullable=False),
        _ts("updated_at", server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("severity IN ('low','medium','high')", name="ck_alert_severity"),
        sa.CheckConstraint("state IN ('open','acknowledged','resolved')", name="ck_alert_state"),
        sa.CheckConstraint("delivery IN ('pending','sent','no_channel','failed')", name="ck_alert_delivery"),
    )
    op.create_index("ix_alert_org_state", "alerts", ["org_id", "state", "raised_at"])
    op.create_index("ix_alert_fingerprint", "alerts", ["org_id", "fingerprint", "state"])
    # At most one live (open / acknowledged) alert per rule + job: two sweeps racing cannot raise it twice.
    op.create_index("uq_alert_live_fingerprint", "alerts", ["org_id", "fingerprint"], unique=True,
                    postgresql_where=sa.text("state IN ('open','acknowledged')"))
    op.create_table(
        "alert_events", _id(),
        sa.Column("alert_id", UUID(as_uuid=True), sa.ForeignKey("alerts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("action", sa.String(200), nullable=False),
        sa.Column("actor", sa.String(255), nullable=False),
        sa.Column("note", sa.Text()),
        _ts("at", nullable=False),
    )
    op.create_index("ix_alert_event_alert", "alert_events", ["alert_id", "at"])
    op.create_table(
        "alert_state",
        sa.Column("org_id", sa.String(255), primary_key=True),
        sa.Column("defaults_seeded", sa.Boolean(), nullable=False, server_default=sa.false()),
        _ts("leased_until"),
        _ts("last_sweep_at"),
        sa.Column("last_sweep_ok", sa.Boolean()),
        sa.Column("last_sweep_detail", sa.Text()),
    )


def downgrade() -> None:
    op.drop_table("alert_state")
    op.drop_index("ix_alert_event_alert", table_name="alert_events")
    op.drop_table("alert_events")
    op.drop_index("uq_alert_live_fingerprint", table_name="alerts")
    op.drop_index("ix_alert_fingerprint", table_name="alerts")
    op.drop_index("ix_alert_org_state", table_name="alerts")
    op.drop_table("alerts")
    op.drop_index("ix_alert_observation_job", table_name="alert_observations")
    op.drop_table("alert_observations")
    op.drop_table("alert_rules")
    op.drop_table("alert_channels")
