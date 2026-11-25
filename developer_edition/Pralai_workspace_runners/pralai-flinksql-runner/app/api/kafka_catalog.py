"""Kafka topic catalog API — browse topics, schemas, preview messages."""
from __future__ import annotations
import structlog
from fastapi import APIRouter, HTTPException, Query, status
from app.core.deps import RequireCatalogBrowse, RequireDatasetPreview
from app.kafka.client import check_topic_access, filter_topics, get_kafka_client
from app.schemas import KafkaTopicInfo, KafkaTopicPreview, KafkaMessagePreview, SqlTemplateOut
from app.services.preview_audit import record_access, record_result

log = structlog.get_logger()
router = APIRouter(prefix="/api/v1/kafka", tags=["kafka-catalog"])

SQL_TEMPLATES = [
    SqlTemplateOut(
        name="Kafka JSON source",
        description="Flink CREATE TABLE for a JSON Kafka topic with event-time watermark",
        category="source",
        placeholders=["TABLE_NAME", "KAFKA_TOPIC", "COLUMN_DEFS", "TS_COLUMN"],
        sql="""CREATE TABLE {TABLE_NAME} (
  {COLUMN_DEFS},
  event_ts TIMESTAMP(3),
  WATERMARK FOR event_ts AS event_ts - INTERVAL '5' SECOND
) WITH (
  'connector' = 'kafka',
  'topic' = '{KAFKA_TOPIC}',
  'properties.bootstrap.servers' = '${KAFKA_BOOTSTRAP_SERVERS}',
  'properties.group.id' = '${CONSUMER_GROUP_ID}',
  'scan.startup.mode' = 'latest-offset',
  'format' = 'json',
  'json.timestamp-format.standard' = 'ISO-8601'
);""",
    ),
    SqlTemplateOut(
        name="Kafka Avro source (Schema Registry)",
        description="Flink CREATE TABLE for an Avro Kafka topic using Confluent Schema Registry",
        category="source",
        placeholders=["TABLE_NAME", "KAFKA_TOPIC", "COLUMN_DEFS"],
        sql="""CREATE TABLE {TABLE_NAME} (
  {COLUMN_DEFS}
) WITH (
  'connector' = 'kafka',
  'topic' = '{KAFKA_TOPIC}',
  'properties.bootstrap.servers' = '${KAFKA_BOOTSTRAP_SERVERS}',
  'properties.group.id' = '${CONSUMER_GROUP_ID}',
  'scan.startup.mode' = 'latest-offset',
  'format' = 'avro-confluent',
  'avro-confluent.url' = '${SCHEMA_REGISTRY_URL}'
);""",
    ),
    SqlTemplateOut(
        name="Iceberg sink (Trino / Polaris)",
        description="Flink CREATE TABLE targeting an Iceberg table via the Polaris REST catalog",
        category="sink",
        placeholders=["TABLE_NAME", "SCHEMA_NAME", "TARGET_TABLE", "COLUMN_DEFS"],
        sql="""CREATE TABLE {TABLE_NAME} (
  {COLUMN_DEFS}
) WITH (
  'connector' = 'iceberg',
  'catalog-name' = 'inaiera',
  'catalog-type' = 'rest',
  'uri' = '${POLARIS_REST_URI}',
  'warehouse' = '${ICEBERG_WAREHOUSE}',
  'database-name' = '{SCHEMA_NAME}',
  'table-name' = '{TARGET_TABLE}',
  'write.format.default' = 'parquet',
  'write.target-file-size-bytes' = '134217728'
);""",
    ),
    SqlTemplateOut(
        name="pg_lake PostgreSQL sink",
        description=("Write into an EXISTING pg_lake table through the runner's governed `pg_lake` JDBC catalog. "
                     "The connection (URL, user, password) is configured on the runner and never written in SQL."),
        category="sink",
        placeholders=["DATABASE", "PG_TABLE", "SELECT_LIST", "SOURCE_TABLE"],
        sql="""-- The `pg_lake` catalog is a named connection owned by the runner: do not put a URL, user or password in SQL.
INSERT INTO pg_lake.{DATABASE}.{PG_TABLE}
SELECT {SELECT_LIST}
FROM {SOURCE_TABLE};""",
    ),
    SqlTemplateOut(
        name="Tumbling window aggregation",
        description="Aggregate events in fixed 5-minute windows with event-time semantics",
        category="transformation",
        placeholders=["SOURCE_TABLE", "SINK_TABLE", "GROUP_KEY"],
        sql="""INSERT INTO {SINK_TABLE}
SELECT
  {GROUP_KEY},
  COUNT(*) AS event_count,
  SUM(amount) AS total_amount,
  AVG(amount) AS avg_amount,
  TUMBLE_START(event_ts, INTERVAL '5' MINUTE) AS window_start,
  TUMBLE_END(event_ts, INTERVAL '5' MINUTE) AS window_end
FROM {SOURCE_TABLE}
GROUP BY
  {GROUP_KEY},
  TUMBLE(event_ts, INTERVAL '5' MINUTE);""",
    ),
    SqlTemplateOut(
        name="Sliding window with late event handling",
        description="1-hour sliding window, allow 10-min late arrivals, emit on watermark",
        category="transformation",
        placeholders=["SOURCE_TABLE", "SINK_TABLE", "GROUP_KEY"],
        sql="""INSERT INTO {SINK_TABLE}
SELECT
  {GROUP_KEY},
  SUM(amount) OVER (
    PARTITION BY {GROUP_KEY}
    ORDER BY event_ts
    RANGE BETWEEN INTERVAL '1' HOUR PRECEDING AND CURRENT ROW
  ) AS rolling_1h_total,
  event_ts
FROM {SOURCE_TABLE};""",
    ),
    SqlTemplateOut(
        name="Kafka → Iceberg full job",
        description="Complete end-to-end: JSON Kafka source → windowed aggregation → Iceberg sink",
        category="full_job",
        placeholders=["KAFKA_TOPIC", "SCHEMA_NAME", "TARGET_TABLE"],
        sql="""-- Source: Kafka JSON topic
CREATE TABLE kafka_source (
  order_id     BIGINT,
  customer_id  BIGINT,
  amount       DECIMAL(10, 2),
  status       STRING,
  event_ts     TIMESTAMP(3),
  WATERMARK FOR event_ts AS event_ts - INTERVAL '10' SECOND
) WITH (
  'connector'                           = 'kafka',
  'topic'                               = '{KAFKA_TOPIC}',
  'properties.bootstrap.servers'        = '${KAFKA_BOOTSTRAP_SERVERS}',
  'properties.group.id'                 = '${CONSUMER_GROUP_ID}',
  'scan.startup.mode'                   = 'latest-offset',
  'format'                              = 'json'
);

-- Sink: Iceberg via Polaris REST catalog
CREATE TABLE iceberg_sink (
  customer_id  BIGINT,
  order_count  BIGINT,
  total_amount DECIMAL(10, 2),
  window_start TIMESTAMP(3),
  window_end   TIMESTAMP(3)
) WITH (
  'connector'     = 'iceberg',
  'catalog-name'  = 'inaiera',
  'catalog-type'  = 'rest',
  'uri'           = '${POLARIS_REST_URI}',
  'warehouse'     = '${ICEBERG_WAREHOUSE}',
  'database-name' = '{SCHEMA_NAME}',
  'table-name'    = '{TARGET_TABLE}'
);

-- Transformation: 5-minute tumbling windows
INSERT INTO iceberg_sink
SELECT
  customer_id,
  COUNT(*)        AS order_count,
  SUM(amount)     AS total_amount,
  TUMBLE_START(event_ts, INTERVAL '5' MINUTE) AS window_start,
  TUMBLE_END(event_ts, INTERVAL '5' MINUTE)   AS window_end
FROM kafka_source
WHERE status = 'completed'
GROUP BY
  customer_id,
  TUMBLE(event_ts, INTERVAL '5' MINUTE);""",
    ),
]


@router.get("/topics", response_model=list[KafkaTopicInfo])
async def list_topics(user: RequireCatalogBrowse) -> list[KafkaTopicInfo]:
    """RUN-FLINK-016: only topics this org/user may see (org topic patterns
    + UCS policy), and the listing is audited."""
    kc = get_kafka_client()
    topics, reason = await filter_topics(user.org_id, user.user_id, await kc.list_topics())
    await record_access(org_id=user.org_id, user_id=user.user_id, action="list_topics", topic=None,
                        allowed=reason is None, reason=reason, message_count=len(topics))
    return [KafkaTopicInfo(**t) for t in topics]


@router.get("/topics/{topic}/schema")
async def get_topic_schema(topic: str, user: RequireCatalogBrowse) -> dict:
    allowed, reason = await check_topic_access(user.org_id, user.user_id, topic, "schema")
    await record_access(org_id=user.org_id, user_id=user.user_id, action="schema", topic=topic,
                        allowed=allowed, reason=reason)
    if not allowed:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail={"code": "SCHEMA_DENIED",
                                    "message": reason or "Schema access not permitted for this topic"})
    kc = get_kafka_client()
    schema = await kc.get_topic_schema(topic)
    return schema or {"message": "No schema found in Schema Registry for this topic"}


@router.get("/topics/{topic}/preview", response_model=KafkaTopicPreview)
async def preview_topic(
    topic: str, user: RequireDatasetPreview,
    max_messages: int = Query(default=10, ge=1, le=50),
) -> KafkaTopicPreview:
    # RUN-FLINK-016: org scoping + UCS policy (fail closed outside dev), and
    # a DURABLE audit row written before any data is read.
    allowed, reason = await check_topic_access(user.org_id, user.user_id, topic, "preview")
    audit_id = await record_access(org_id=user.org_id, user_id=user.user_id, action="preview",
                                   topic=topic, allowed=allowed, reason=reason)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "PREVIEW_DENIED",
                    "message": reason or "Preview not permitted for this topic"},
        )

    kc = get_kafka_client()
    result = await kc.preview(topic, max_messages)
    await record_result(audit_id, message_count=len(result["messages"]),
                        bytes_returned=result.get("bytes", 0))
    schema = await kc.get_topic_schema(topic)
    return KafkaTopicPreview(
        topic=topic,
        messages=[KafkaMessagePreview(**m) for m in result["messages"]],
        schema_inferred=schema.get("schema") if schema else None,
        truncated=result.get("truncated", False),
        bytes_returned=result.get("bytes", 0),
    )


@router.get("/templates", response_model=list[SqlTemplateOut])
async def list_sql_templates(
    user: RequireCatalogBrowse,
    category: str | None = Query(None),
) -> list[SqlTemplateOut]:
    if category:
        return [t for t in SQL_TEMPLATES if t.category == category]
    return SQL_TEMPLATES
