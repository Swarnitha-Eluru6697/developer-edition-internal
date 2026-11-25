# Quality modes: OBSERVE and ENFORCE

A quality policy is applied to a pipeline in one of two **modes**. Pick one per pipeline.

| | **OBSERVE** (default) | **ENFORCE** |
|---|---|---|
| What the quality worker does | Evaluates each record, keeps the statistics, copies a failing record to the quarantine topic | Same, plus writes every **passing** record to the pipeline's **prepared topic** |
| The data topic | Untouched. It still carries **every** record, failures included | Untouched too, but consumers must not read it |
| Where a failing record goes | Quarantine (a copy; it also stays on the data topic) | Quarantine **only** |
| What a consumer that reads the data topic sees | All records, valid or not | (not allowed for sinks, see below) |
| What a consumer that reads the prepared topic sees | Nothing: there is no prepared topic | Only valid records |

**OBSERVE is a record of what failed, not a barrier.** Quarantine tells you something went wrong; it does not stop the bad record reaching
whatever reads the data topic. If invalid records must never reach a consumer, use ENFORCE.

Every pipeline created before this existed is OBSERVE, which is exactly what it already did. Nothing changes until someone chooses ENFORCE.

## How ENFORCE works

```
 source ──► Kafka Connect ──► data topic  raw-erp-sap-orders-cdc-v1       (every record, unchanged)
                                   │
                          quality worker  (applies the policy to each record)
                           ┌───────┴────────┐
                     passes│                │fails
                           ▼                ▼
        prepared topic  prepared-erp-sap-orders-cdc-v1     quarantine  quarantine-erp-sap-orders-validation-v1
                           │
                  sink connectors read THIS
```

- The **quarantine topic** of a source uses mode `validation`; a **sink**'s uses mode `validation_sink`
  (`quarantine-shop-pg-products_sink-validation_sink-v1`), so the name tells which side of the flow a failure came from.
- The **prepared topic** is the pipeline's own six-part name with the zone replaced by `prepared`. The API returns it as `prepared_topic` on
  the pipeline (it is `null` in OBSERVE).
- A record is in exactly one place: prepared **or** quarantine. A passing record is written as the very same message Kafka Connect wrote (its
  envelope and key are kept), so a sink reads it exactly as it would have read the original.
- The record is acknowledged by the broker before the worker commits its offset. If the write keeps failing, the record goes to the worker
  dead-letter topic (reason `prepared_produce_failed`) with the full original message, so it can be replayed. If even that is unavailable the
  worker stalls rather than skip the record. Delivery is **at-least-once**: after a crash, a record can appear in the prepared topic twice.
- **If the worker is down, the prepared topic stops growing.** A sink then waits; it never receives an invalid record.

## Who reads what

- A **sink pipeline in ENFORCE mode** is deployed reading its own prepared topic. The topic you configured as its input is what the worker
  reads; the sink connector never sees it.
- A **sink pipeline may not read the data topic of an ENFORCE pipeline** (creating or editing one refuses it). The error names the prepared
  topic to read instead. Because switching a source to ENFORCE is refused while a sink reads its data topic, no sink can end up on the wrong
  topic through the API.
- A **source pipeline cannot be switched to ENFORCE while a sink still reads its data topic**. Point those sinks at the prepared topic first
  (a sink may name a prepared topic before it exists), then switch the source.
- The prepared topic is created when the pipeline is deployed, so a sink never subscribes to a topic that is not there yet.

## When ENFORCE is allowed

The API refuses (422, field `quality_mode`) unless **all** of these hold:

1. The pipeline has a quality policy. The database also enforces this.
2. The policy routes failures to quarantine (`on_failure = route_to_quarantine`) and uses the Kafka quarantine sink. A failing record has to go
   somewhere other than the prepared topic.
3. The pipeline's zone is not `quarantine` (the quality worker does not check that zone).
4. The prepared topic is not the pipeline's own data topic or one of its input topics (for example a source whose zone is already `prepared`).

If the policy is edited later so it no longer routes failures, an ENFORCE worker **still quarantines** failing records and logs a warning. The
only alternatives are dropping them or forwarding them, and either would break the guarantee.

**BLOCK_ON_FAILURE** (stop the stream when a record fails) is deliberately not a mode. It is a different policy with different consequences,
and would be its own decision. A policy whose `on_failure` is `block` cannot be used with ENFORCE.

## Using it

API: set `quality_mode` to `OBSERVE` or `ENFORCE` when creating a pipeline, or in a pipeline update. `OBSERVE` is the default.

UI: in the pipeline form, under **Quality policy**, choose **How the policy is applied**. It is disabled until a policy is chosen, and
removing the policy puts it back to Observe.

Verify with 10 records, 3 invalid: in ENFORCE the prepared topic gets 7 and quarantine gets 3; in OBSERVE the prepared topic gets nothing,
quarantine gets 3, and the data topic still holds all 10.

## Delivery guarantees: what a crash can repeat

Kafka delivers at-least-once. The quality worker records a record's statistics and publishes its output **before** it commits the Kafka offset, so a
crash in between makes the restarted worker receive the same record again. The guarantees, stated plainly:

| Thing | Guarantee | How |
|---|---|---|
| Statistics: totals, failed count, the failure sample, per-rule counts | **Exactly once** | The record's `(pipeline, topic, partition, offset)` is claimed in the **same transaction** as the statistics (`dc_quality_stream_offsets`). A replay finds it already claimed and counts nothing. |
| Quarantine, prepared and dead-letter **messages** | **At least once** | The worker cannot know whether a copy reached Kafka before the crash, so it publishes it again. Every message carries a stable identity so a consumer can drop the repeat. |
| A batch job's quarantine messages for the chunk in flight when its worker died | At least once | Same: `inai.dedupe_key` = `<job id>:<chunk>:<position>`. |
| A batch job's **result** | **Exactly one per job** | `dc_quality_results.job_id` is unique and the write is idempotent. |

**Stable identity for deduplication.** A message the stream worker publishes carries the header **`inai.dedupe_key`** =
`<pipeline id>:<topic>:<partition>:<offset>` of the source record: the same on every delivery of the same record. The quarantine and dead-letter
payloads also carry **`event_id`** = `<topic>:<partition>:<offset>`, and `original_topic` / `original_partition` / `original_offset`. A consumer that
needs each record once keeps the keys it has seen and discards a message whose key it has already processed. The key includes the pipeline because two
pipelines can read the same topic, and each publishes its own message for the same source record.

A replayed record is visible: `quality_stream.replay_not_recounted` in the log and `dataconnect_quality_replays_skipped_total`.

**A topic that was deleted and recreated** restarts at offset 0, so a stored offset beyond the topic's end can only mean that; the worker drops such
offsets when it starts. Otherwise the new records would be treated as already counted.

**What is not protected.** A crash after the offset claim but before the quarantine copy is published is retried on the replay (the copy is
published then, once or twice, never zero times). The statistics count the record once either way.

## A pipeline that reads several topics

A sink may read more than one topic (`connector_config.topics` is comma-separated or a list). The quality worker subscribes to **all** of them
with one consumer group, so every input topic is evaluated and quarantined, and in ENFORCE every passing record from every topic goes to the
pipeline's single prepared topic.

Every record the worker writes says where it was read from, because "the pipeline's topic" no longer identifies it:

| Written to | Carries |
|---|---|
| Quarantine, worker dead letter, the failure sample on the Quality page | `original_topic`, `original_partition`, `original_offset` |
| The prepared topic (message headers) | `inai.source_topic`, `inai.source_partition`, `inai.source_offset` |

Records from the same topic and partition keep their order in the prepared topic; records from different topics are interleaved in no
particular order. The Quality page's failed-records drawer shows the topic, partition and offset of each failure.

**What was wrong before (NEW-02).** The worker passed the whole `topics` setting (`a,b,c`) to Kafka as one topic name. Kafka rejects that as
illegal on every poll; the worker logged it and retried every second, so a sink with two or more input topics had **none** of them checked.
A consumer now refuses a comma-joined name up front instead of retrying it forever. `data_topic()` still returns only the first topic, but it
is for labels only; `consumed_topics()` decides what to read.

## A bug this uncovered

Running the real worker against a real broker showed that a failing record whose `id` is a **number** (a database primary key, the normal
case) was never quarantined: the Kafka key serializer only accepts text, so the produce raised and the record was dead-lettered. The key is now
the id as text (`app/services/record_key.py`; `0` is a valid id), and the in-memory test producer rejects a non-text key as the real one does.

## Where the code is

| What | File |
|---|---|
| Modes, the prepared topic, when ENFORCE is allowed | `app/services/quality_mode.py` |
| The worker (forwarding, quarantine, dead letter) | `app/services/quality_stream_worker.py` |
| Create/edit rules and the sink checks | `app/services/pipeline_spec_validator.py` |
| What a sink connector reads | `app/services/connect_config.py` (`_sink_topic`) |
| Prepared topic created at deploy | `app/services/pipeline_service.py` |
| Lineage shows the prepared hop only for ENFORCE | `app/services/lineage_service.py` |
| Column and constraints | `alembic/versions/0048_pipeline_quality_mode.py` |
| Kafka key for a routed record | `app/services/record_key.py` |
| Every topic a worker reads | `app/services/pipeline_topics.py` (`consumed_topics`), `app/services/kafka_runtime.py` (`NativeKafkaConsumer`) |

Tests: `test_quality_enforce_mode.py`, `test_quality_enforce_pipeline_flow.py`, `test_quality_multi_topic.py`, `test_record_key.py`, and the migration tests in
`test_migrations_real_db.py`. Frontend: `tests/e2e/pipelines.spec.ts` (NEW-01).
