"""Topic Policy model — explicit Kafka topic-provisioning configuration."""

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UuidPk


class TopicPolicy(Base, UuidPk, TimestampMixin):
    """
    Explicit Kafka topic-provisioning configuration for the topics a
    pipeline's source connector creates — partitions, replication factor,
    retention, cleanup policy, min in-sync replicas, compression, maximum
    message size, and schema strategy.

    app/services/topic_naming.py is a good foundation for topic NAMING, but
    naming alone left every one of these knobs to whatever the Kafka Connect
    worker / broker happened to default to — an implicit, unrecorded choice
    that silently varied by cluster. This model makes that choice explicit
    and reviewable, and resolvable at two levels (see
    app/services/topic_policy.py's resolve_topic_config):

        1. Pipeline-level — a pipeline sets `topic_policy_id` directly.
        2. Domain-level   — a policy with is_domain_default=True and `domain`
           set is the fallback for every pipeline in that domain that didn't
           pick its own policy.
        3. Platform defaults (settings.topic_default_*) — used for any field
           left null at every level above; still an explicit, configured
           value, never a bare "whatever the broker feels like."

    Every numeric/enum field is nullable: a policy only needs to override
    the fields it actually cares about, and inherits the rest.
    """

    __tablename__ = "dc_topic_policies"

    org_id: Mapped[str] = mapped_column(ForeignKey("dc_organizations.org_id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)

    domain: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
        comment="When is_domain_default is set, the domain this policy is the fallback for",
    )
    is_domain_default: Mapped[bool] = mapped_column(Boolean, default=False)

    partitions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    replication_factor: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="Null = use the platform default replication factor"
    )
    retention_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retention_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cleanup_policy: Mapped[str | None] = mapped_column(
        String(20), nullable=True, comment="delete | compact | compact,delete"
    )
    min_insync_replicas: Mapped[int | None] = mapped_column(Integer, nullable=True)
    compression_type: Mapped[str | None] = mapped_column(
        String(20), nullable=True, comment="producer | gzip | snappy | lz4 | zstd | uncompressed"
    )
    max_message_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    schema_strategy: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
        comment="topic_name | record_name | topic_record_name (Schema Registry subject-naming strategy)",
    )
