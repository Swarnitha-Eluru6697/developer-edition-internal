from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UuidPk


class FailureCount(Base, UuidPk):
    """FE-BUG-17: exact, windowed failure counts for the Quality screen's two
    failure groups, kept as one-minute buckets rather than read back out of a
    bounded sample.

    Why this exists: validation failures used to be counted from
    QualityStreamStats.sample_failures (a ring buffer of the newest 50), and
    ingestion causes from a bounded read of the newest DLQ records — so a
    pipeline failing faster than the sample holds was undercounted. Every
    failing record now increments a bucket here as it happens
    (QualityStreamWorker for validation, DlqFailureCounter for ingestion), and
    a window is just a SUM over buckets: exact at any volume, in O(buckets)
    not O(records).

    kind = "validation": key is a quality rule name, or "*" for the number of
           failing RECORDS (a record that broke two rules adds 1 to "*" and 1
           to each rule).
    kind = "ingestion":  key is the cause category (serialization,
           schema_conversion, connector).

    Ingestion counts are exactly-once per Kafka record (NEW-03): the highest offset counted for each partition is stored in
    FailureCountOffset in the SAME transaction as the increments, so a crash between that commit and the Kafka offset commit replays the
    batch and the replay is recognised and skipped. Validation counts are still written by the quality worker with its own commit and are
    at-least-once. Buckets older than settings.failure_count_retention_days are pruned.
    """

    __tablename__ = "dc_failure_counts"
    __table_args__ = (
        UniqueConstraint("pipeline_id", "kind", "key", "bucket_start", name="uq_dc_failure_counts_bucket"),
        Index("ix_dc_failure_counts_org_kind_bucket", "org_id", "kind", "bucket_start"),
    )

    org_id: Mapped[str] = mapped_column(ForeignKey("dc_organizations.org_id"), index=True)
    pipeline_id: Mapped[str] = mapped_column(ForeignKey("dc_pipelines.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20))
    key: Mapped[str] = mapped_column(String(255))
    bucket_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    count: Mapped[int] = mapped_column(BigInteger, default=0)


class FailureCountOffset(Base, TimestampMixin):
    """NEW-03 / NEW-04: how far into each Kafka partition of a topic the failure counters already reach — the offset of the NEXT record that has
    not been counted. Written in the same database transaction as the count increments it covers, which is what makes a replay harmless.

    Without it the DLQ counter committed the counts to PostgreSQL and THEN committed its Kafka offsets; a crash in between made the consumer
    group restart from the older offset and count the same records again, so error-rate dashboards overstated failures. Now PostgreSQL is the
    authority on what has been counted and Kafka's committed offset is only a hint about where to start reading: a record at an offset below
    `next_offset` was counted already and is skipped, whoever reads it and however many times it is redelivered.

    Keyed by (kind, topic, partition) — the TOPIC, not a pipeline: a connector dead-letter topic can be shared by several pipelines, each record
    is attributed to its own pipeline (app/services/dlq_attribution.py), and "every record below this offset has been read and counted for whoever
    it belongs to" is a fact about the topic. Keying it by pipeline would lose the progress when one of them is deleted and count the rest twice.

    A Kafka topic that is deleted and recreated starts again from offset 0, so a stored offset beyond the topic's end can only mean that;
    the counter drops such rows when it starts (dlq_failure_counter.forget_offsets_past_topic_end), or the new records would be skipped.
    """

    __tablename__ = "dc_failure_count_offsets"

    kind: Mapped[str] = mapped_column(String(20), primary_key=True)
    topic: Mapped[str] = mapped_column(String(255), primary_key=True)
    kafka_partition: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    org_id: Mapped[str] = mapped_column(ForeignKey("dc_organizations.org_id", ondelete="CASCADE"), index=False)
    next_offset: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
