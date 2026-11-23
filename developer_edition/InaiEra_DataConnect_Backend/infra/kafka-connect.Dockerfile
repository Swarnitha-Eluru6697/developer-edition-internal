# Kafka Connect worker image, with the connector plugins baked in (jars downloaded at build).
# Build context is the infra/ folder (so the local-file jar can be COPYed).
#
# POOL selects which plugins go in (runtime design: one Connect cluster per workload — app/core/connect_pools.py).
# A pool's image carries ONLY what its workload needs: plugin isolation (a CDC pool never loads a sink's classes),
# a smaller attack surface, and an upgrade of one connector family touches one pool.
#
#   --build-arg POOL=all      every plugin (the default; the shared single-cluster dev worker)
#   --build-arg POOL=cdc      Debezium: postgres, mysql, mongodb, sqlserver, oracle
#   --build-arg POOL=jdbc     Confluent JDBC (source) + MySQL driver, MongoDB
#   --build-arg POOL=file     file and object-store sources: local connect-file, S3 source
#   --build-arg POOL=api      none baked in yet (REST/webhook plugins are added here)
#   --build-arg POOL=stream   none baked in yet (JMS / MQ / syslog plugins are added here)
#   --build-arg POOL=sink     every sink: JDBC, S3, Elasticsearch, MongoDB, Iceberg, local connect-file
FROM confluentinc/cp-kafka-connect:7.6.0@sha256:8e904b33d2693e3e251023b3d09448e30f49b38b1f1cd882883c30323a25585d

ARG POOL=all

USER root

# ── Confluent Hub connectors (versions verified against a working worker) ────
# Each plugin is installed only for the pools that run it (see the table above).
RUN set -eux; \
    case "$POOL" in all|jdbc|sink) confluent-hub install --no-prompt confluentinc/kafka-connect-jdbc:10.9.5 ;; esac; \
    case "$POOL" in all|sink) confluent-hub install --no-prompt confluentinc/kafka-connect-s3:10.5.19 ;; esac; \
    case "$POOL" in all|file) confluent-hub install --no-prompt confluentinc/kafka-connect-s3-source:2.6.3 ;; esac; \
    case "$POOL" in all|sink) confluent-hub install --no-prompt confluentinc/kafka-connect-elasticsearch:15.1.2 ;; esac; \
    case "$POOL" in all|jdbc|sink) confluent-hub install --no-prompt mongodb/kafka-connect-mongodb:3.1.0 ;; esac; \
    # Redis Sink: the connector class the tpl_redis_sink template names (connect_config_t1.REDIS_SINK).
    case "$POOL" in all|sink) confluent-hub install --no-prompt jcustenborder/kafka-connect-redis:0.0.5 ;; esac

# ── Debezium CDC connectors (Postgres / MySQL / MongoDB / SQL Server / Oracle) ─
ARG DBZ=2.5.4.Final
# Debezium MySQL: 2.5.4 like the others. 3.0.8 (needed for MySQL 8.4, which removed SHOW MASTER STATUS) is built for Java 17, and this
# image's worker runs Java 11: Connect logged UnsupportedClassVersionError and never loaded the class, so no MySQL CDC pipeline could run
# (2026-11-02). With 2.5.4, MySQL CDC works against 5.7 / 8.0 (not 8.4) — the catalog says so (app/db/seed_templates.py). MySQL 8.4
# needs a Java 17 Connect base image first.
ARG DBZ_MYSQL=2.5.4.Final
RUN set -eux; mkdir -p /usr/share/confluent-hub-components; cd /tmp; \
    case "$POOL" in all|cdc) \
      for c in postgres mysql mongodb sqlserver oracle; do \
        v=$DBZ; [ "$c" = mysql ] && v=$DBZ_MYSQL; \
        curl -fsSL "https://repo1.maven.org/maven2/io/debezium/debezium-connector-$c/${v}/debezium-connector-$c-${v}-plugin.tar.gz" \
          | tar -xz -C /usr/share/confluent-hub-components/; \
      done ;; \
    esac

# ── MySQL JDBC driver (the Confluent JDBC connector needs it on its classpath) ─
RUN case "$POOL" in all|jdbc|sink) \
      curl -fsSL -o /usr/share/confluent-hub-components/confluentinc-kafka-connect-jdbc/lib/mysql-connector-j-8.4.0.jar \
        https://repo1.maven.org/maven2/com/mysql/mysql-connector-j/8.4.0/mysql-connector-j-8.4.0.jar ;; \
    esac

# ── Local File source/sink (Apache Kafka's connect-file, not in the CP image) ──
RUN mkdir -p /usr/share/confluent-hub-components/connect-file
COPY connect-plugins/connect-file-3.6.1.jar /usr/share/confluent-hub-components/connect-file/
RUN case "$POOL" in all|file|sink) ;; *) rm -rf /usr/share/confluent-hub-components/connect-file ;; esac

# ── Apache Iceberg sink (getindata/kafka-connect-iceberg-sink, not on Confluent
# Hub) — ships as a flat zip of the connector + every dependency (Hadoop, Hive
# metastore, AWS SDK, Iceberg's own classes via the iceberg-spark-runtime fat
# jar), ~470MB uncompressed. Extracted with the JDK's own `jar` tool since this
# RHEL UBI base image has no `unzip`.
RUN mkdir -p /usr/share/confluent-hub-components && cd /usr/share/confluent-hub-components \
 && case "$POOL" in all|sink) \
      curl -fsSL -o /tmp/iceberg-sink.zip \
        https://github.com/getindata/kafka-connect-iceberg-sink/releases/download/0.4.0/kafka-connect-iceberg-sink-0.4.0-plugin.zip \
      && jar xf /tmp/iceberg-sink.zip \
      && rm /tmp/iceberg-sink.zip ;; \
    esac

# ── Prometheus JMX exporter (every pool) ─────────────────────────────────────
# Serves the worker's Kafka Connect JMX metrics on :9404/metrics with the rules in jmx-exporter/kafka-connect.yml
# (per-connector `connector`/`task` labels), for Prometheus to scrape — the Monitoring page's per-connector metrics
# (app/services/connector_prometheus_metrics.py) are queried from there.
#
# KAFKA_OPTS is inherited by `docker exec` too: run a Kafka CLI tool inside the container as
# `KAFKA_OPTS= kafka-topics ...`, or the second JVM fails to bind :9404.
ARG JMX_EXPORTER_VERSION=1.0.1
RUN mkdir -p /opt/jmx-exporter \
 && curl -fsSL -o /opt/jmx-exporter/jmx_prometheus_javaagent.jar \
      "https://repo1.maven.org/maven2/io/prometheus/jmx/jmx_prometheus_javaagent/${JMX_EXPORTER_VERSION}/jmx_prometheus_javaagent-${JMX_EXPORTER_VERSION}.jar"
COPY jmx-exporter/kafka-connect.yml /opt/jmx-exporter/kafka-connect.yml
ENV KAFKA_OPTS="-javaagent:/opt/jmx-exporter/jmx_prometheus_javaagent.jar=9404:/opt/jmx-exporter/kafka-connect.yml"
EXPOSE 9404

USER appuser
