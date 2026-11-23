"""Alerts: Slack / Teams channels, the rules that raise alerts, what the sweep saw, the alerts with their history, and per-organization
sweep state. See app/services/alerts/ and docs/ALERTS_IMPLEMENTATION_GUIDE.md.

Ported from the Workspace (InaiEra_workspace_backend, migration 0015_alerts). Differences: everything is per organization (`org_id`
foreign-keys dc_organizations like every other dc_ table), what is watched is a PIPELINE (its id / name / direction), and a channel's
webhook URL is encrypted in this database (dc_alert_channel_secrets, app/services/secret_crypto.py) instead of a secrets backend.
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UuidPk


class AlertChannel(Base, UuidPk, TimestampMixin):
    """Where alerts are posted: a Slack or Microsoft Teams incoming webhook. The URL is a credential (anyone holding it can post into the
    channel), so it is NOT kept here: it is encrypted in AlertChannelSecret; only a short `url_hint` ("hooks.slack.com/…/x7Q2") is."""

    __tablename__ = "dc_alert_channels"
    __table_args__ = (
        CheckConstraint("kind IN ('slack','teams')", name="ck_dc_alert_channels_kind"),
        CheckConstraint("min_severity IN ('low','medium','high')", name="ck_dc_alert_channels_severity"),
        # Case-insensitive: "Ops" and "ops" are the same channel to a person.
        Index("uq_dc_alert_channels_org_name", "org_id", text("lower(name)"), unique=True),
    )

    org_id: Mapped[str] = mapped_column(ForeignKey("dc_organizations.org_id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(10))
    url_hint: Mapped[str] = mapped_column(String(200), default="")
    min_severity: Mapped[str] = mapped_column(String(10), default="low")
    # One pipeline, or NULL = every pipeline. The name is a snapshot for display.
    scope_pipeline_id: Mapped[str | None] = mapped_column(String(64))
    scope_pipeline_name: Mapped[str | None] = mapped_column(String(255))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_ok: Mapped[bool | None] = mapped_column(Boolean)
    last_detail: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(255))


class AlertChannelSecret(Base):
    """A channel's webhook URL, AES-256-GCM encrypted (secret_crypto.encrypt_fields, bound to the org and the channel id). Never logged,
    never returned by the API."""

    __tablename__ = "dc_alert_channel_secrets"

    channel_id: Mapped[str] = mapped_column(ForeignKey("dc_alert_channels.id", ondelete="CASCADE"), primary_key=True)
    envelope: Mapped[dict] = mapped_column(JSONB)


class AlertRule(Base, UuidPk, TimestampMixin):
    """What raises an alert: a kind of trouble, how much of it, over which window, for which pipeline. Evaluated by every sweep
    (app/services/alerts/engine.py)."""

    __tablename__ = "dc_alert_rules"
    __table_args__ = (
        CheckConstraint("severity IN ('low','medium','high')", name="ck_dc_alert_rules_severity"),
        CheckConstraint("threshold > 0", name="ck_dc_alert_rules_threshold"),
        CheckConstraint("min_runs >= 1", name="ck_dc_alert_rules_min_runs"),
        Index("uq_dc_alert_rules_org_name", "org_id", text("lower(name)"), unique=True),
    )

    org_id: Mapped[str] = mapped_column(ForeignKey("dc_organizations.org_id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(40))
    severity: Mapped[str] = mapped_column(String(10), default="medium")
    threshold: Mapped[int] = mapped_column(Integer, default=1)
    window_seconds: Mapped[int] = mapped_column(Integer, default=0)    # 0 = the kind's default
    min_runs: Mapped[int] = mapped_column(Integer, default=1)          # kept from the Workspace shape; no DataConnect kind uses it yet
    scope_pipeline_id: Mapped[str | None] = mapped_column(String(64))
    scope_pipeline_name: Mapped[str | None] = mapped_column(String(255))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[str | None] = mapped_column(String(255))


class AlertObservation(Base, UuidPk):
    """One thing the sweep saw happen to a pipeline: its connector changed state, a deploy / pause / resume / restart failed, the error
    threshold paused it. Rules are evaluated over these, so a short failure between two sweeps is not lost. Unique per (source, ref_id)."""

    __tablename__ = "dc_alert_observations"
    __table_args__ = (
        UniqueConstraint("org_id", "source", "ref_id", name="uq_dc_alert_observations_ref"),
        Index("ix_dc_alert_observations_pipeline", "org_id", "pipeline_id", "occurred_at"),
    )

    org_id: Mapped[str] = mapped_column(ForeignKey("dc_organizations.org_id"))
    source: Mapped[str] = mapped_column(String(20))           # runtime | operation | audit
    ref_id: Mapped[str] = mapped_column(String(300))
    pipeline_id: Mapped[str] = mapped_column(String(64))
    pipeline_name: Mapped[str] = mapped_column(String(255))
    direction: Mapped[str] = mapped_column(String(10))         # SOURCE | SINK
    category: Mapped[str] = mapped_column(String(20))          # state | operation | threshold
    outcome: Mapped[str] = mapped_column(String(30))           # failed | degraded | running | paused | unknown | ...
    detail: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))


class Alert(Base, UuidPk, TimestampMixin):
    """One raised alert (a rule breached for a pipeline). open -> acknowledged -> resolved; it is also resolved by itself when the
    condition clears. While one is open or acknowledged the same breach raises no other (no channel flooding)."""

    __tablename__ = "dc_alerts"
    __table_args__ = (
        CheckConstraint("severity IN ('low','medium','high')", name="ck_dc_alerts_severity"),
        CheckConstraint("state IN ('open','acknowledged','resolved')", name="ck_dc_alerts_state"),
        CheckConstraint("delivery IN ('pending','sent','no_channel','failed')", name="ck_dc_alerts_delivery"),
        Index("ix_dc_alerts_org_state", "org_id", "state", "raised_at"),
        Index("ix_dc_alerts_fingerprint", "org_id", "fingerprint", "state"),
        # At most one live (open / acknowledged) alert per rule + pipeline: two sweeps racing cannot raise it twice.
        Index("uq_dc_alerts_live_fingerprint", "org_id", "fingerprint", unique=True,
              postgresql_where=text("state IN ('open','acknowledged')")),
    )

    org_id: Mapped[str] = mapped_column(ForeignKey("dc_organizations.org_id"))
    rule_id: Mapped[str | None] = mapped_column(ForeignKey("dc_alert_rules.id", ondelete="SET NULL"))
    rule_name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(40))
    severity: Mapped[str] = mapped_column(String(10))
    pipeline_id: Mapped[str] = mapped_column(String(64))
    pipeline_name: Mapped[str] = mapped_column(String(255))
    direction: Mapped[str] = mapped_column(String(10))
    summary: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(String(200))       # "<rule id>:<pipeline id>"
    state: Mapped[str] = mapped_column(String(15), default="open")
    # pending = not tried yet / retrying; sent = at least one channel got it; no_channel = none matches (yet); failed = every matching
    # channel refused it (retried by later sweeps, a few times).
    delivery: Mapped[str] = mapped_column(String(12), default="pending")
    delivery_attempts: Mapped[int] = mapped_column(Integer, default=0)
    raised_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # When the newest thing behind the breach happened. After a person resolves an alert by hand, the same rule raises again for that
    # pipeline only on NEWER evidence, not on the same failure still being there (docs: D4.1).
    evidence_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_ref_id: Mapped[str | None] = mapped_column(String(300))

    events: Mapped[list["AlertEvent"]] = relationship(back_populates="alert", cascade="all, delete-orphan", order_by="AlertEvent.at")


class AlertEvent(Base, UuidPk):
    """History of an alert: raised, sent to a channel, delivery failed, acknowledged, resolved, cleared automatically."""

    __tablename__ = "dc_alert_events"
    __table_args__ = (Index("ix_dc_alert_events_alert", "alert_id", "at"),)

    alert_id: Mapped[str] = mapped_column(ForeignKey("dc_alerts.id", ondelete="CASCADE"))
    action: Mapped[str] = mapped_column(String(200))
    actor: Mapped[str] = mapped_column(String(255))
    note: Mapped[str | None] = mapped_column(Text)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    alert: Mapped[Alert] = relationship(back_populates="events")


class AlertState(Base):
    """One row per organization: whether its default rules were created, and the sweep lease (so several replicas never sweep at once)."""

    __tablename__ = "dc_alert_state"

    org_id: Mapped[str] = mapped_column(ForeignKey("dc_organizations.org_id"), primary_key=True)
    defaults_seeded: Mapped[bool] = mapped_column(Boolean, default=False)
    leased_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_sweep_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_sweep_ok: Mapped[bool | None] = mapped_column(Boolean)
    last_sweep_detail: Mapped[str | None] = mapped_column(Text)
