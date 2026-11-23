"""Alerts API: rules, channels, alerts. A channel's webhook URL goes IN (create / replace) and is never returned."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Severity = Literal["low", "medium", "high"]
AlertState = Literal["open", "acknowledged", "resolved"]
Delivery = Literal["pending", "sent", "no_channel", "failed"]
ChannelKind = Literal["slack", "teams"]
RuleKindName = Literal["run_failed", "consecutive_failures", "failure_rate", "quality_failed", "upstream_blocked", "flink_job_failed"]
JobKindName = Literal["dbt", "sql", "python", "flink"]

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
Note = Annotated[str, StringConstraints(strip_whitespace=True, max_length=1000)]


class _Out(BaseModel):
    # Response-only: every field is always serialised, so mark the defaulted ones required in the OpenAPI schema too.
    model_config = ConfigDict(from_attributes=True, json_schema_serialization_defaults_required=True)


# ── rules ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────
class RuleKindInfo(_Out):
    kind: RuleKindName
    label: str
    description: str
    threshold_hint: str
    uses_window: bool
    uses_min_runs: bool
    max_threshold: int


class RuleOut(_Out):
    id: uuid.UUID
    name: str
    kind: RuleKindName
    severity: Severity
    threshold: int
    window_seconds: int
    min_runs: int
    scope_job_key: str | None
    scope_job_name: str | None
    enabled: bool
    created_by: str | None
    created_at: datetime
    updated_at: datetime


class RuleCreate(BaseModel):
    name: Name
    kind: RuleKindName
    severity: Severity = "medium"
    threshold: int = Field(default=1, ge=1, le=1000)
    window_seconds: int = Field(default=0, ge=0)
    min_runs: int = Field(default=1, ge=1, le=100_000)
    scope_job_key: Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)] | None = None
    scope_job_name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)] | None = None
    enabled: bool = True


class RulePatch(BaseModel):
    name: Name | None = None
    kind: RuleKindName | None = None
    severity: Severity | None = None
    threshold: int | None = Field(default=None, ge=1, le=1000)
    window_seconds: int | None = Field(default=None, ge=0)
    min_runs: int | None = Field(default=None, ge=1, le=100_000)
    # "" clears the scope (all jobs); omitted leaves it alone.
    scope_job_key: Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)] | None = None
    scope_job_name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)] | None = None
    enabled: bool | None = None


class Breaching(_Out):
    rule_id: uuid.UUID
    rule_name: str
    kind: RuleKindName
    severity: Severity
    job_key: str
    job_name: str
    job_kind: JobKindName
    summary: str
    # already_open: reported and still open / acknowledged, so nothing new is sent; would_raise: the next sweep raises it.
    state: Literal["already_open", "would_raise"]


# ── channels ───────────────────────────────────────────────────────────────────────────────────────────────────────────────
class ChannelOut(_Out):
    id: uuid.UUID
    name: str
    kind: ChannelKind
    url_hint: str
    min_severity: Severity
    scope_job_key: str | None
    scope_job_name: str | None
    enabled: bool
    last_attempt_at: datetime | None
    last_ok: bool | None
    last_detail: str | None
    created_by: str | None
    created_at: datetime
    updated_at: datetime


class ChannelCreate(BaseModel):
    name: Name
    kind: ChannelKind
    webhook_url: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
    min_severity: Severity = "low"
    scope_job_key: Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)] | None = None
    scope_job_name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)] | None = None
    enabled: bool = True


class ChannelPatch(BaseModel):
    name: Name | None = None
    kind: ChannelKind | None = None
    # Omitted / blank keeps the stored URL (it is never shown again once saved).
    webhook_url: Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)] | None = None
    min_severity: Severity | None = None
    scope_job_key: Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)] | None = None
    scope_job_name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)] | None = None
    enabled: bool | None = None


class ChannelTestResult(_Out):
    ok: bool
    detail: str
    channel: ChannelOut


# ── alerts ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────
class AlertEventOut(_Out):
    action: str
    actor: str
    note: str | None
    at: datetime


class AlertOut(_Out):
    id: uuid.UUID
    rule_id: uuid.UUID | None
    rule_name: str
    kind: RuleKindName
    severity: Severity
    job_key: str
    job_name: str
    job_kind: JobKindName
    summary: str
    state: AlertState
    delivery: Delivery
    raised_at: datetime
    sent_at: datetime | None
    resolved_at: datetime | None
    last_action: str
    last_actor: str
    last_at: datetime


class AlertDetail(AlertOut):
    events: list[AlertEventOut]


class AlertsPage(_Out):
    items: list[AlertOut]
    total: int
    # Open alerts no channel has received (yet): none matches, or delivery failed. Not in the Open list until sent.
    undelivered: int


class AlertCounts(_Out):
    open: int
    acknowledged: int
    resolved: int
    undelivered: int


class AlertAction(BaseModel):
    note: Note | None = None


class SweepOut(_Out):
    ran: bool
    detail: str
    raised: int
    cleared: int
    delivered: int
    sources: list[str]
