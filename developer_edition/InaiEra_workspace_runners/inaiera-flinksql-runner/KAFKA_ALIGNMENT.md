# Kafka Cluster Alignment: Dataconnect ↔ FlinkSQL Runner

## The same Kafka cluster

Both Dataconnect (EL layer) and the FlinkSQL Runner (stream-T layer) connect
to the SAME Kafka cluster. Dataconnect writes topics; Flink reads them.

```
External Sources
     │
     ▼
Dataconnect ──writes──► Kafka Topics ──reads──► FlinkSQL Jobs ──writes──► Lakehouse
     (EL)                (shared)               (stream-T)                (Iceberg)
```

## Shared secret

A single Kubernetes Secret `kafka-client-credentials` is created once at
data-plane provisioning time and referenced by ALL services:

```yaml
# Created by the data-plane provisioning Terraform/Helm
apiVersion: v1
kind: Secret
metadata:
  name: kafka-client-credentials
  namespace: dp-shared
stringData:
  username: "inaiera-dataplane-client"
  password: "<scram-sha-512-password>"
```

Services that reference it:
  - Dataconnect:        secretKeyRef: kafka-client-credentials
  - FlinkSQL Runner:    secretKeyRef: kafka-client-credentials  (KAFKA_SASL_USERNAME/PASSWORD; legacy KAFKA_USERNAME/PASSWORD also accepted)
  - Flink TaskManagers: secretKeyRef: kafka-client-credentials  (env vars in pod template)
  - Flink SQL Gateway:  secretKeyRef: kafka-client-credentials  (env vars)
  - KEDA TriggerAuth:   secretTargetRef: kafka-client-credentials

## Topic naming convention

Dataconnect writes to topics using a consistent naming convention:
  raw.{source}.{entity}     e.g.  raw.postgres.orders
                                   raw.salesforce.contacts
                                   raw.stripe.events

FlinkSQL source tables reference these topics:
  'topic' = 'raw.postgres.orders'

## ACL / authorization

Each service uses the same user but Kafka ACLs restrict access:
  - Dataconnect:     WRITE on raw.*
  - FlinkSQL Runner: READ on raw.*, WRITE on none (writes go to Iceberg)
  - Flink TaskMgrs:  READ on raw.*, consumer group CREATE/DESCRIBE

The ACL configuration lives in the Dataconnect Helm chart's provisioning
job and is applied at deploy time via `kafka-acls.sh`.

## Consumer group IDs

The FlinkSQL Runner auto-generates consumer group IDs per job:
  inaiera-flink-{job_id}

These are registered in Kafka ACLs via a prefix rule:
  ALLOW READ on consumer group prefix: inaiera-flink-*

## Schema Registry

Both Dataconnect (for Avro topics it produces) and FlinkSQL Runner (for Avro
sources) use the same Schema Registry instance:
  http://schema-registry.dp-shared.svc.cluster.local:8081

Dataconnect registers schemas when writing Avro topics. FlinkSQL Runner reads
them when generating CREATE TABLE DDL for users in the Kafka Catalog browser.
