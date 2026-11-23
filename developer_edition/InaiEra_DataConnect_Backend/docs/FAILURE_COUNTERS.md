# Failure counters: what is counted, and how often

The Quality screen and the monitoring dashboards show how many records failed, and why. Two things write those numbers into
`dc_failure_counts` (one-minute buckets, summed over a window when read).

| Counter | What it counts | Written by | Counted |
|---|---|---|---|
| **Ingestion** (`kind = ingestion`) | Records Kafka Connect put on a sink's dead-letter topic, by cause (serialization, schema conversion, connector) | `DlqCounterWorker` | **Exactly once** per Kafka record |
| **Validation** (`kind = validation`) | Records that broke a quality rule, per rule | `QualityStreamWorker` | **Exactly once** per Kafka record (NEW-05) |

## How the ingestion counter stays exact

Kafka delivery is at-least-once: a consumer commits its offset **after** it has handled a batch, so if it dies in between, the restarted
consumer is handed the same records again. The counter used to write its counts to PostgreSQL and **then** commit the Kafka offset, so a crash
between the two counted that batch twice and error-rate dashboards overstated failures.

Now PostgreSQL, not Kafka, is the authority on what has been counted:

1. In **one transaction** the counter locks the stored offsets for the partitions in the batch, skips every record whose offset is already
   covered, adds the counts for the rest, and moves the stored offsets forward (`dc_failure_count_offsets`: the next offset not yet counted,
   per pipeline and partition). The counts and the offsets commit together, or neither does.
2. **Only then** does it commit the Kafka offset.

```
poll batch ─► [ BEGIN  lock offsets ─► drop already-counted ─► add counts ─► advance offsets  COMMIT ] ─► commit Kafka offset
                         one transaction                                                                  (a crash HERE is now harmless)
```

A crash after step 1 and before step 2 restarts the consumer from the older Kafka offset; the same records arrive, are recognised as counted and
skipped. The same protection covers a reset consumer group, a partition moving to another replica, and two replicas handed the same records at
once (the second waits on the row lock, then sees the first one's offsets).

- A record is skipped only if it is below the stored offset, so a genuinely new record is never lost.
- The offsets are kept per **pipeline**, topic and partition. If a shared dead-letter topic changes owner, the new owner counts it from the start,
  as it always did. Rows go with their pipeline.
- **A deleted and recreated topic.** A recreated topic restarts at offset 0, so a stored offset beyond the topic's end can only mean that. When a
  counter starts it drops such offsets (log `dlq_count.topic_recreated_offsets_reset`), otherwise the new records would be skipped. A topic
  recreated while the counter is running, and refilled past the stored offset before it restarts, cannot be told apart from the old one.

## A dead-letter topic shared by several pipelines (NEW-04)

A connector DLQ topic is named from domain, source system, entity and version only, so two sink pipelines that differ only in their zone or mode
would write the **same** topic. Creating or editing a pipeline into such a conflict is refused (GAP-04), and since NEW-08 the **database** enforces it
too: a partial unique index on a sink's dead-letter identity means two concurrent creates cannot both succeed (the loser gets the same validation
error, naming the pipeline that owns the queue). One can still exist from before that rule (and the migration refuses to run until it is resolved), or
through rows written outside the API. The counter used to treat the **first** pipeline as the owner of a shared topic and file every record under it: the other
pipelines always read zero, the first read the sum, and deleting the first pipeline wiped everyone's counts (`ON DELETE CASCADE`).

Each record is now counted under the pipeline **whose connector wrote it**. Kafka Connect stamps `__connect.errors.connector.name` on every DLQ
record (`errors.deadletterqueue.context.headers.enable`, which DataConnect sets), and a pipeline's `connector_name` is unique per organization.

| The record | Counted under |
|---|---|
| Names one of the topic's pipelines | That pipeline |
| Names a connector that is not one of them (a deleted pipeline's leftovers) | No pipeline |
| Has no connector header, and the topic has exactly one pipeline | That pipeline |
| Has no connector header, and the topic is shared | No pipeline: it could be any of them |

A record counted for no pipeline is logged (`dlq_count.records_not_attributed`) and counted in `dataconnect_dlq_unattributed_records_total`, so
the gap is visible instead of silent. The topic still has **one** consumer; its set of pipelines is refreshed on every reconcile, so a pipeline
added to or removed from a shared topic changes who gets the counts without restarting it.

Because one topic now serves several pipelines, the "already counted" offsets belong to the **topic** (`kind, topic, partition`), not to a pipeline
(migration 0050; existing rows keep the highest offset per topic and partition). Deleting a pipeline therefore no longer makes the rest of the
topic count again.

**Reading per pipeline.** `GET /runtime/metrics/failures?pipeline_id=<id>` narrows both groups to one pipeline: its own exact counters, and only
its own recent records, whose pipeline name now comes from the record, not from the topic. For ingestion, `total` is then the sum of that
pipeline's counters (a topic's offset total cannot be split per pipeline without reading every record).

**Still topic-level:** the THRESHOLD auto-pause counts messages on the DLQ topic directly (`error_threshold.py`), so on a shared topic one
pipeline's failures still count toward another's threshold. New sinks cannot share a topic (GAP-04), which is why this has been left alone.

## The "recent failures" list (NEW-06)

The Quality screen's recent connector failures are read straight off the DLQ topic (headers only; the exact counts above never depend on it).
`read_recent_connector_errors(topic, window, limit)` honours **one limit for the whole topic**, not one per partition. Before, it took the newest
`limit` records of every partition, so `limit=200` on a 20-partition topic returned up to 4,000 records, in whatever order the partitions were polled.
Now:

- the newest `limit` records of each partition are the **candidates** (so at most `limit` x partitions records are read, never a partition's whole history),
  and only the `limit` newest of those are kept, in a heap that never holds more than `limit`;
- they come back **newest first**, with a fixed tie-break (timestamp, then lower partition, then higher offset), so records that failed in the same
  millisecond always come out in the same order and a smaller limit is exactly the top of a larger one;
- `limit` is capped at 1000, and 0 or less returns nothing without opening a connection;
- each record says where it came from (`partition`, `offset`).

Candidates are chosen by log position and ordered by timestamp. Connect stamps a DLQ record when it writes it, so the two agree; a producer that stamped
records more than `limit` positions out of order within one partition could have one missed.

## Validation counts (NEW-05)

The quality worker writes a record's statistics and its per-rule validation counts in one transaction and commits its Kafka offset afterwards, so a
crash in between used to count the replayed record twice on the Quality page. The record's `(pipeline, topic, partition, offset)` is now claimed in that
same transaction (`dc_quality_stream_offsets`, per **pipeline**, because two pipelines can read one topic and each counts the record for itself),
and a replay counts nothing. See `QUALITY_MODES.md` for what a crash can still repeat (the published messages, which carry a stable dedupe key).

## Tests

`tests/test_connector_dlq_reader.py` and `tests/test_connector_dlq_reader_real_kafka.py` (NEW-06: `limit=200` on 20 partitions returns at most 200, the
newest, in a stable order),
`tests/test_quality_replay_identity.py`, `tests/test_quality_stream_offsets_real_db.py`, `tests/test_quality_job_result_once_real_db.py` and
`tests/test_quality_replay_real_kafka.py` (NEW-05: a replay after a crash leaves the statistics unchanged and the repeats identifiable; a job has one
result),
`tests/test_dlq_attribution.py`, `tests/test_dlq_shared_topic_monitoring.py` and `tests/test_dlq_shared_topic_real_db.py` (NEW-04: two pipelines on
one DLQ counted separately, unattributable records, the manager following the topic, deleting the first pipeline, the per-pipeline read),
`tests/test_dlq_counter_exactly_once_real_db.py` (real PostgreSQL: replay after commit, overlap, partitions, rollback, locking, topic recreation,
and the worker's commit order) and `tests/test_dlq_counter_real_kafka.py` (real Kafka and PostgreSQL: the crash is injected on the Kafka commit,
the counter reads 10 before and after the replay, and new records are still counted). Both need `DATACONNECT_REAL_DB_TESTS=1`; the second also
needs `DATACONNECT_REAL_KAFKA_TESTS=1`.
