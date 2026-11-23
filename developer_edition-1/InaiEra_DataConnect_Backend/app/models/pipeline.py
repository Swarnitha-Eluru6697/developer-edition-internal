"""Pipeline models — the core ingestion unit and its execution history."""

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.error_policies import DLQ_POLICIES
from app.db.base import Base, TimestampMixin, UuidPk

#: NEW-08: the unique index that makes "one sink per dead-letter topic" a fact the database enforces, not only a check the API makes first.
DEAD_LETTER_TOPIC_INDEX = "uq_dc_pipelines_dead_letter_topic"
TOPIC_NAME_INDEX = "uq_dc_pipelines_topic_name"
CONNECTOR_NAME_INDEX = "uq_dc_pipelines_connector_name"


class Pipeline(Base, UuidPk, TimestampMixin):
    """
    A configured, deployable data ingestion pipeline.

    A pipeline binds together:
        - A ConnectorTemplate (which Kafka Connect plugin to use)
        - A ConnectionProfile (credentials/config for the source system)
        - Optionally: a TransformPolicy, SchemaVersion, and QualityPolicy

    The pipeline_config service generates `topic_name`, `connector_name`,
    and `generated_config` deterministically from the 7-part naming convention:
        <org>-<zone>-<domain>-<source_system>-<entity>-<mode>-<version>   ("." for pipelines created before the switch)

    Unique constraints prevent duplicate topic or connector names per org,
    which would cause silent data corruption in Kafka.

    Status lifecycle (enforced by the service state machine):
        draft → deployed → running ↔ paused → restarting → failed → deployed
    """

    __tablename__ = "dc_pipelines"
    __table_args__ = (
        UniqueConstraint("org_id", "topic_name"),
        # GAP-12: direction decides the row's shape; see alembic/versions/0040_pipeline_direction_integrity.py.
        CheckConstraint("direction IN ('SOURCE', 'SINK')", name="ck_dc_pipelines_direction"),
        # GAP (runtime design): a connector runs in one of the platform's worker pools (app/core/connect_pools.py).
        CheckConstraint(
            "runtime_pool_id IS NULL OR runtime_pool_id IN ('connect-cdc-pool', 'connect-jdbc-pool', 'connect-file-pool', "
            "'connect-api-pool', 'connect-stream-pool', 'connect-sink-pool')",
            name="ck_dc_pipelines_runtime_pool",
        ),
        CheckConstraint(
            "(direction = 'SINK' AND topic_name IS NULL) OR (direction = 'SOURCE' AND topic_name IS NOT NULL)",
            name="ck_dc_pipelines_topic_by_direction",
        ),
        CheckConstraint(
            "direction <> 'SINK' OR coalesce(connector_config ->> 'topics', '') <> ''",
            name="ck_dc_pipelines_sink_input_topic",
        ),
        CheckConstraint(
            "direction <> 'SOURCE' OR NOT jsonb_exists(connector_config, 'topics')",
            name="ck_dc_pipelines_source_has_no_topics_setting",
        ),
        # NEW-01: how the quality policy is applied (see app/services/quality_mode.py). ENFORCE is meaningless without a policy.
        CheckConstraint("quality_mode IN ('OBSERVE', 'ENFORCE')", name="ck_dc_pipelines_quality_mode"),
        CheckConstraint("quality_mode <> 'ENFORCE' OR quality_policy_id IS NOT NULL", name="ck_dc_pipelines_enforce_needs_policy"),
        CheckConstraint("record_format IN ('v1', 'v2')", name="ck_dc_pipelines_record_format"),
        UniqueConstraint("org_id", "connector_name"),
        # NEW-08: a sink's connector dead-letter topic is named from domain / source system / entity / version ONLY (the topic naming contract has no
        # room for zone or mode), so two sinks that differ only in zone or mode write ONE topic. The API refuses the second up front, but checking and
        # inserting are separate steps, so two concurrent creates could both pass the check. This index is the part that cannot race: whichever
        # transaction commits second is refused. Only a sink with a dead-letter policy owns such a topic (Kafka Connect gives a source none).
        # Keep the policy list in step with app/core/error_policies.DLQ_POLICIES (tests/test_dead_letter_uniqueness.py checks it).
        # Since 2026-11-06 (0057): the topic names carry no organization, so the index is unique ACROSS organizations — two organizations that name a sink
        # alike would otherwise write one topic.
        Index(
            DEAD_LETTER_TOPIC_INDEX, "domain", "source_system", "entity", "version", unique=True,
            postgresql_where=text("direction = 'SINK' AND error_handling_policy IN (" + ", ".join(f"'{p}'" for p in sorted(DLQ_POLICIES)) + ")"),
        ),
        # Likewise the data topic and the connector name (0057): per organization they were unique because the org id made them distinct; now they are
        # unique across organizations, so two organizations on one Kafka / Connect cluster cannot create the same topic or connector.
        Index(TOPIC_NAME_INDEX, "topic_name", unique=True, postgresql_where=text("topic_name IS NOT NULL")),
        Index(CONNECTOR_NAME_INDEX, "connector_name", unique=True),
    )

    org_id: Mapped[str] = mapped_column(ForeignKey("dc_organizations.org_id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)

    # Foreign keys to associated resources
    template_id: Mapped[str] = mapped_column(ForeignKey("dc_connector_templates.id"))
    connection_profile_id: Mapped[str] = mapped_column(ForeignKey("dc_connection_profiles.id"))
    transform_policy_id: Mapped[str | None] = mapped_column(ForeignKey("dc_transform_policies.id"))
    schema_version_id: Mapped[str | None] = mapped_column(ForeignKey("dc_schema_versions.id"))
    quality_policy_id: Mapped[str | None] = mapped_column(ForeignKey("dc_quality_policies.id"))
    quality_mode: Mapped[str] = mapped_column(
        String(10), default="OBSERVE", server_default="OBSERVE",
        comment="How the quality policy is applied: OBSERVE (count and copy failures to quarantine; the data topic is unchanged) | "
                "ENFORCE (passing records to the prepared topic, failures to quarantine)",
    )
    # Not a database foreign key: TopicPolicy CRUD is still memory-store-backed
    # in this codebase (out of scope for this migration), so a Postgres FK to
    # dc_topic_policies could never resolve against an id that only exists in
    # memory — the same reason SchemaDriftApproval/LineageEdge leave
    # comparable cross-references unconstrained elsewhere in this codebase.
    topic_policy_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False))
    # The exact ConnectorTemplateRevision this pipeline was built against —
    # see ConnectorTemplateRevision's docstring (app/models/catalog.py) and
    # _build_pipeline_row below for why this is pinned at create/retarget
    # time rather than always reading the template's current revision.
    # Revision rows are immutable/append-only, so a real FK here is safe.
    template_revision_id: Mapped[str | None] = mapped_column(
        ForeignKey("dc_connector_template_revisions.id")
    )

    # Topic naming parts (stored denormalised for fast lookup / filtering)
    zone: Mapped[str] = mapped_column(String(50))
    domain: Mapped[str] = mapped_column(String(100))
    source_system: Mapped[str] = mapped_column(String(100))
    entity: Mapped[str] = mapped_column(String(100))
    mode: Mapped[str] = mapped_column(String(50))
    version: Mapped[str] = mapped_column(String(50))
    # GAP-12: derived from the resolved ConnectorTemplate.connector_type at
    # create/retarget time (see PipelineService._build_pipeline_row,
    # pipeline_spec_validator.py's _validate_direction) — never
    # user-submitted. Source and sink connectors have genuinely different
    # structural requirements (connect_config.py's _build_connector has a
    # distinct branch per class, never shared); this makes that explicit
    # and queryable on the pipeline itself instead of only inferable by
    # joining to its template.
    direction: Mapped[str] = mapped_column(String(10), comment="SOURCE | SINK")

    # Generated values — set by pipeline_config.generate_pipeline_config()
    # GAP-12: the topic a SOURCE produces to; NULL for a SINK, which generates none (it reads connector_config["topics"]).
    topic_name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    connector_name: Mapped[str] = mapped_column(String(500))
    connector_config: Mapped[dict] = mapped_column(JSONB, default=dict)
    generated_config: Mapped[dict] = mapped_column(
        JSONB,
        default=dict,
        comment="Full Kafka Connect config payload, ready to POST to /connectors",
    )
    # The 12 revision-relevant template fields pinned at create/retarget
    # time — see ConnectorTemplateRevision and _build_pipeline_row. Read by
    # deploy()/the reconciler instead of the live template, so an unrelated
    # template edit can never retroactively reshape an already-created
    # pipeline's deploy.
    template_snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)

    # The worker pool this pipeline's connector runs in (e.g. "connect-cdc-pool") — one of app/core/connect_pools.py's
    # POOLS. DERIVED from the connector template when the pipeline is created, never client-chosen, and fixed for the
    # pipeline's life (a connector cannot move between Connect clusters). Stored as a plain string, not a UUID.
    runtime_pool_id: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(50), default="draft")
    # FE-BUG-25: the Operation this pipeline is currently mid-transition
    # for, if any — set atomically alongside `status` by
    # PipelineService.claim_lifecycle_transition at submission time (not a
    # hard foreign key; see 0032's migration docstring for why), cleared
    # once status settles back to a stable value (_persist_pipeline_fields).
    # Lets the frontend resume polling the right operation after a page
    # refresh instead of only knowing "something is in flight."
    active_operation_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(255))

    # GAP-04: how this pipeline's connector reacts to a bad record — see
    # app/schemas/pipeline.py's VALID_ERROR_HANDLING_POLICIES for what each
    # of the three values means, app/services/pipeline_config.py's
    # error_handling_connect_config for how it becomes real Kafka Connect
    # config, and app/services/error_threshold.py + runtime_reconciler.py
    # for THRESHOLD's own enforcement (Connect itself has no "N errors per
    # window" concept, so that one is this app's own monitoring logic).
    error_handling_policy: Mapped[str] = mapped_column(
        String(50), default="DLQ_AND_CONTINUE", comment="FAIL_FAST | SKIP_AND_LOG | DLQ_AND_CONTINUE | THRESHOLD (the last two are sink-only)",
    )
    # Only meaningful when error_handling_policy is THRESHOLD.
    error_threshold_max_errors: Mapped[int | None] = mapped_column()
    error_threshold_window_seconds: Mapped[int | None] = mapped_column()

    # BUG-10: the outcome of the best-effort schema auto-introspection run at
    # pipeline creation (see PipelineService.autocreate_schema_from_source) —
    # previously an exception there (e.g. the source connection timing out)
    # was silently swallowed by the create_pipeline route with no record of
    # it anywhere. Null means introspection was never attempted (the pipeline
    # was created with an explicit schema_version_id already set).
    schema_introspection_status: Mapped[str | None] = mapped_column(
        String(20), nullable=True,
        comment="linked_existing | created | skipped | failed — see autocreate_schema_from_source",
    )
    schema_introspection_message: Mapped[str | None] = mapped_column(
        Text, nullable=True,
        comment="Sanitized reason for skipped/failed — e.g. 'Source connection timed out'",
    )

    # How the connector writes (or reads) records, fixed when the pipeline is created (migration 0056; app/services/connect_config.py):
    #   v1  what every pipeline did before 2026-11-02: a JDBC source sends NUMERIC/DECIMAL as best_fit (NUMERIC with a scale becomes a double;
    #       MySQL's DECIMAL stays an exact decimal), a CDC source writes the Debezium envelope as plain JSON (no schema, so no database sink can
    #       read it), a database sink only inserts.
    #   v2  exact decimals everywhere; a JDBC source keys each record by the table's primary key; a CDC source writes the row itself with its
    #       schema (deletes as __deleted plus a tombstone); a database sink upserts by that key (and deletes, for CDC) when its input carries it.
    # Never changed afterwards: the reconciler rebuilds every connector's config on each tick, so changing what v1 means would rewrite the
    # topics and break the tables of pipelines already running.
    record_format: Mapped[str] = mapped_column(String(8), default="v2", server_default="v2")

    # FE-BUG-14: optimistic-concurrency counter for the editable spec (see
    # EDITABLE_SPEC_FIELDS in pipeline_service.py) — bumped only by
    # update_pipeline() when it actually applies a change. Deliberately NOT
    # touched by deploy/pause/resume/restart or the runtime reconciler:
    # those mutate runtime state (status, generated_config, ...), not the
    # spec two people could be mid-editing at once — bumping it on every
    # reconciler tick would make "someone else changed this" fire for
    # reasons that have nothing to do with editing. Not to be confused with
    # `version` above (the topic-identity part, e.g. "v1" — user-chosen,
    # immutable once set).
    revision: Mapped[int] = mapped_column(default=1, server_default="1")


class PipelineRun(Base, UuidPk):
    """
    A single execution record for a pipeline (snapshot, batch, or CDC window).

    Written by the runtime orchestrator or a monitoring consumer.
    Used to calculate throughput metrics and error rates on the dashboard.
    """

    __tablename__ = "dc_pipeline_runs"

    org_id: Mapped[str] = mapped_column(ForeignKey("dc_organizations.org_id"), index=True)
    # CASCADE, not the FK's implicit RESTRICT default: pipeline_id is NOT NULL
    # here, so SET NULL isn't an option — a run record has no meaning once
    # its pipeline is gone, so it should be removed along with it rather
    # than block the delete (see QualityExecutionJob.pipeline_id, same case).
    pipeline_id: Mapped[str] = mapped_column(
        ForeignKey("dc_pipelines.id", ondelete="CASCADE"), index=True
    )
    run_type: Mapped[str] = mapped_column(
        String(50),
        comment="snapshot | batch | cdc_window | manual",
    )
    status: Mapped[str] = mapped_column(
        String(50),
        comment="running | completed | failed | cancelled",
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    records_read: Mapped[int] = mapped_column(default=0)
    records_written: Mapped[int] = mapped_column(default=0)
    records_failed: Mapped[int] = mapped_column(default=0)
    error_message: Mapped[str | None] = mapped_column(Text)
    runtime_metadata: Mapped[dict] = mapped_column(
        JSONB,
        default=dict,
        comment="Kafka Connect task IDs, offsets, worker node, etc.",
    )
