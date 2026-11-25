# ==============================================================================
# InAiEra FlinkSQL Runner — self-contained single image ("dbt-runner style")
#
# This ONE image bundles everything needed to run FlinkSQL jobs:
#
#   ┌─────────────────────────────────────────────┐
#   │  Apache Flink 1.19.1                         │
#   │    ├─ JobManager   (REST :8081)              │
#   │    ├─ TaskManager  (task slots)              │
#   │    └─ SQL Gateway  (REST :8083)              │
#   │  + all SQL connectors (Kafka/Iceberg/JDBC)   │
#   │  FastAPI Runner API (:8080)                  │
#   └─────────────────────────────────────────────┘
#
# Just like the dbt runner connects to an external warehouse, this image
# connects to external data systems (Kafka, Postgres metadata DB, Redis).
# The compute engine (Flink) travels *inside* the image — there is no
# separate Flink cluster to deploy.
#
# Build:  docker build -f docker/Dockerfile -t pralai-flinksql-runner:latest .
# Run:    docker run -p 8080:8080 -p 8081:8081 -p 8083:8083 \
#            -e DATABASE_URL=postgresql+asyncpg://flinksql:flinksql@host:5432/flinksql \
#            -e KAFKA_BOOTSTRAP_SERVERS=host:9092 \
#            pralai-flinksql-runner:latest
# ==============================================================================
FROM apache/flink:1.19.1-scala_2.12-java17

# ── Connector versions (kept in sync with the platform's Dockerfile.flink) ────
ARG KAFKA_CONNECTOR_VERSION=3.2.0-1.19
ARG ICEBERG_VERSION=1.6.1
ARG JDBC_CONNECTOR_VERSION=3.2.0-1.19
ARG FLINK_VERSION=1.19.1

USER root

# ── 1. SQL connectors — placed on the Flink classpath (/opt/flink/lib) ────────
# Kafka source/sink connector. Only the SQL uber jar: it bundles kafka-clients
# relocated under org.apache.flink.kafka.shaded. The thin flink-connector-kafka
# jar must NOT sit beside it — both register the same 'kafka' table factory, and
# when the thin one wins it resolves the unshaded org.apache.kafka.clients.*
# names, which nothing on this classpath provides. Every Kafka INSERT then dies
# at runtime with NoClassDefFoundError: .../OffsetResetStrategy.
RUN curl -fsSL \
      "https://repo1.maven.org/maven2/org/apache/flink/flink-sql-connector-kafka/${KAFKA_CONNECTOR_VERSION}/flink-sql-connector-kafka-${KAFKA_CONNECTOR_VERSION}.jar" \
      -o /opt/flink/lib/flink-sql-connector-kafka.jar
# Iceberg Flink runtime — Polaris REST catalog support
RUN curl -fsSL \
      "https://repo1.maven.org/maven2/org/apache/iceberg/iceberg-flink-runtime-1.19/${ICEBERG_VERSION}/iceberg-flink-runtime-1.19-${ICEBERG_VERSION}.jar" \
      -o /opt/flink/lib/iceberg-flink-runtime.jar
# Avro format with Confluent Schema Registry
RUN curl -fsSL \
      "https://repo1.maven.org/maven2/org/apache/flink/flink-sql-avro-confluent-registry/${FLINK_VERSION}/flink-sql-avro-confluent-registry-${FLINK_VERSION}.jar" \
      -o /opt/flink/lib/flink-sql-avro-confluent-registry.jar
# JDBC connector + PostgreSQL driver (pg_lake sink)
RUN curl -fsSL \
      "https://repo1.maven.org/maven2/org/apache/flink/flink-connector-jdbc/${JDBC_CONNECTOR_VERSION}/flink-connector-jdbc-${JDBC_CONNECTOR_VERSION}.jar" \
      -o /opt/flink/lib/flink-connector-jdbc.jar && \
    curl -fsSL "https://jdbc.postgresql.org/download/postgresql-42.7.3.jar" \
      -o /opt/flink/lib/postgresql-jdbc.jar

# S3 filesystem plugin — enables s3:// for checkpoints and the Iceberg warehouse
ENV ENABLE_BUILT_IN_PLUGINS=flink-s3-fs-hadoop-${FLINK_VERSION}.jar

# ── 2. Python 3 + the Runner API (installed into an isolated venv) ────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-venv python3-dev \
      gcc libpq-dev curl \
    && rm -rf /var/lib/apt/lists/*

ENV VIRTUAL_ENV=/opt/venv
RUN python3 -m venv "$VIRTUAL_ENV"
ENV PATH="$VIRTUAL_ENV/bin:$PATH"
RUN pip install --no-cache-dir --upgrade pip

WORKDIR /app
COPY pyproject.toml .
RUN pip install --no-cache-dir -e ".[prod]"

COPY app/ ./app/
COPY migrations/ ./migrations/
COPY alembic.ini .
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
COPY docker/flink-conf.yaml /opt/flink/conf/flink-conf.yaml
RUN chmod +x /usr/local/bin/entrypoint.sh

# Flink daemons, migrations and the API all run as the unprivileged `flink`
# user (uid 9999) that owns /opt/flink. Give it ownership of the app + venv too.
RUN mkdir -p /tmp/flink-checkpoints /tmp/flink-savepoints \
    && chown -R flink:flink /app /opt/venv /opt/flink /tmp/flink-checkpoints /tmp/flink-savepoints

USER flink

# 8080 = Runner API · 8081 = Flink JobManager REST/UI · 8083 = SQL Gateway REST
EXPOSE 8080 8081 8083

HEALTHCHECK --interval=15s --timeout=5s --start-period=90s --retries=10 \
  CMD curl -fsS http://localhost:8080/healthz || exit 1

# The base image's ENTRYPOINT is Flink's docker-entrypoint.sh — override it.
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
