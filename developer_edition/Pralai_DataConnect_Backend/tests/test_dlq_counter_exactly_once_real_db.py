"""
NEW-03, against a REAL PostgreSQL: the DLQ failure counter counts each Kafka record exactly once, even when the process dies between committing
its counts to the database and committing its offsets to Kafka.

The window: counts were written to PostgreSQL, THEN the Kafka offset was committed. A crash in between restarts the consumer from the older
offset, the same records arrive again, and they were counted again — error-rate dashboards overstated failures. The highest counted offset per
partition is now stored in the SAME transaction as the counts (dc_failure_count_offsets), so a replay is recognised and skipped.

Each test runs the app code in a subprocess against a scratch database (tests/scratch_db.py). Skipped unless DATACONNECT_REAL_DB_TESTS=1.
"""

import json
import os
import subprocess
import sys

import pytest
from scratch_db import BACKEND, scratch_database

pytestmark = pytest.mark.skipif(not os.environ.get("DATACONNECT_REAL_DB_TESTS"), reason="needs a real PostgreSQL (DATACONNECT_REAL_DB_TESTS=1)")

PIPE_A = "00000000-0000-0000-0000-0000000000a2"
PIPE_B = "00000000-0000-0000-0000-0000000000b2"

PRELUDE = '''
import asyncio, json, sys, time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from sqlalchemy import text
from app.db.session import AsyncSessionLocal
from app.services import dlq_failure_counter as dlq
from app.services.dlq_attribution import DlqAttribution

PIPE_A = "00000000-0000-0000-0000-0000000000a2"
PIPE_B = "00000000-0000-0000-0000-0000000000b2"
TOPIC = "quarantine-d-s-e-connector-v1"
OLDEST = datetime.now(UTC) - timedelta(days=1)

ATTR_A = DlqAttribution([{"id": PIPE_A, "connector_name": "c-e"}])        # one pipeline writes this topic

def rec(partition, offset, category="serialization", connector=None):
    ser = category == "serialization"
    headers = [
        ("__connect.errors.stage", (b"VALUE_CONVERTER" if ser else b"TASK_PUT")),
        ("__connect.errors.exception.class.name", (b"org.apache.kafka.connect.errors.DataException" if ser else b"java.sql.SQLException")),
        ("__connect.errors.exception.message", (b"Converting byte[] to Kafka Connect data failed due to serialization error" if ser else b"connection reset")),
    ]
    if connector:
        headers.append(("__connect.errors.connector.name", connector.encode()))
    return SimpleNamespace(topic=TOPIC, partition=partition, offset=offset, timestamp=int(time.time() * 1000), headers=headers)

async def rows(sql):
    async with AsyncSessionLocal() as s:
        return [tuple(r) for r in (await s.execute(text(sql))).all()]

async def total(pipe=PIPE_A):
    return int((await rows(f"SELECT coalesce(sum(count), 0) FROM dc_failure_counts WHERE kind = 'ingestion' AND pipeline_id = '{pipe}'"))[0][0])

async def watermarks():
    return {p: n for p, n in await rows(f"SELECT kafka_partition, next_offset FROM dc_failure_count_offsets WHERE topic = '{TOPIC}' ORDER BY 1")}

async def once(records, attribution=ATTR_A):
    return await dlq.record_batch_once(org_id="org_a", topic=TOPIC, records=records, oldest=OLDEST, attribution=attribution)
'''


def _seed(db) -> None:
    db.execute("""
        INSERT INTO dc_organizations (org_id, name, status) VALUES ('org_a', 'Org A', 'active');
        INSERT INTO dc_connector_templates (id, name, connector_type, category, connector_class, runtime_pool_type, config_schema, default_config,
            default_transforms, version, is_active, supported_transforms, supported_kafka_connect_versions, supported_source_versions,
            transform_compatibility, supported_modes, supports_dlq, supports_schema_discovery)
          VALUES ('tpl_a', 'T', 'sink', 'database', 'x.Y', 'connect-sink-pool', '{}', '{}', '[]', '1', true, '[]', '[]', '[]', '{}', '[]', true, false);
        INSERT INTO dc_connection_profiles (id, org_id, name, source_type, connection_config)
          VALUES ('00000000-0000-0000-0000-0000000000a1', 'org_a', 'conn', 'postgresql', '{}');
    """)
    for pipe, entity in ((PIPE_A, "e"), (PIPE_B, "e2")):
        db.execute(f"""
            INSERT INTO dc_pipelines (id, org_id, name, template_id, connection_profile_id, zone, domain, source_system, entity, mode, version, direction,
                connector_name, connector_config, generated_config, template_snapshot, status, error_handling_policy, runtime_pool_id)
              VALUES ('{pipe}', 'org_a', 'pipe-{entity}', 'tpl_a', '00000000-0000-0000-0000-0000000000a1', 'raw', 'd', 's', '{entity}', 'batch', 'v1',
                      'SINK', 'c-{entity}', '{{"topics": "raw-d-s-in-batch-v1"}}', '{{}}', '{{}}', 'running', 'DLQ_AND_CONTINUE', 'connect-sink-pool');
        """)


@pytest.fixture
def db():
    with scratch_database("dc_dlqonce") as scratch:
        assert scratch.startup_migrations().returncode == 0
        _seed(scratch)
        yield scratch


def _run(scratch, body: str, timeout: int = 120):
    """Runs `body` (an `async def main()` returning something JSON-able) in a subprocess against the scratch database."""
    code = PRELUDE + "\n" + body + "\nprint('RESULT=' + json.dumps(asyncio.run(main()), default=str))\n"
    result = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, env=scratch.env(), capture_output=True, text=True, timeout=timeout)
    assert result.returncode == 0, result.stderr[-2500:]
    line = next(ln for ln in reversed(result.stdout.splitlines()) if ln.startswith("RESULT="))
    return json.loads(line[len("RESULT="):])


# ── The review's scenario ──────────────────────────────────────────────────────────────────────────────────────────────

def test_a_crash_after_the_database_commit_but_before_the_kafka_commit_does_not_count_the_replay(db) -> None:
    result = _run(db, '''
async def main():
    batch = [rec(0, o) for o in range(10)]
    first = await once(batch)                    # the counts and the offsets commit together ...
    after_first = await total()
    # ... the process dies HERE, before it commits its Kafka offset. Kafka redelivers the same ten records to the restarted consumer.
    replay = await once(batch)
    return {"first": [first.counted, first.replayed], "replay": [replay.counted, replay.replayed],
            "after_first": after_first, "after_replay": await total(), "watermarks": await watermarks()}
''')
    assert result["first"] == [10, 0]
    assert result["after_first"] == 10
    assert result["replay"] == [0, 10]                      # recognised as already counted
    assert result["after_replay"] == 10                     # the counter did not increase on replay
    assert result["watermarks"] == {"0": 10}


def test_a_replay_that_overlaps_new_records_counts_only_the_new_ones(db) -> None:
    result = _run(db, '''
async def main():
    await once([rec(0, o) for o in range(6)])
    outcome = await once([rec(0, o) for o in range(4, 12)])      # offsets 4,5 again; 6..11 are new
    return {"outcome": [outcome.counted, outcome.replayed], "total": await total(), "marks": await watermarks()}
''')
    assert result["outcome"] == [6, 2]
    assert result["total"] == 12
    assert result["marks"] == {"0": 12}


def test_each_partition_has_its_own_offset(db) -> None:
    result = _run(db, '''
async def main():
    await once([rec(0, o) for o in range(5)] + [rec(1, o) for o in range(2)])
    outcome = await once([rec(0, 4), rec(1, 1), rec(1, 2), rec(2, 0)])   # p0@4 and p1@1 are old; p1@2 and p2@0 are new
    return {"outcome": [outcome.counted, outcome.replayed], "total": await total(), "marks": await watermarks()}
''')
    assert result["outcome"] == [2, 2]
    assert result["total"] == 9
    assert result["marks"] == {"0": 5, "1": 3, "2": 1}


def test_the_offset_never_moves_backwards(db) -> None:
    result = _run(db, '''
async def main():
    await once([rec(0, o) for o in range(10)])
    await once([rec(0, 3)])                                      # an old record redelivered on its own
    return {"marks": await watermarks(), "total": await total()}
''')
    assert result["marks"] == {"0": 10}
    assert result["total"] == 10


def test_the_causes_are_still_counted_per_category(db) -> None:
    result = _run(db, '''
async def main():
    await once([rec(0, 0, "serialization"), rec(0, 1, "serialization"), rec(0, 2, "connector")])
    await once([rec(0, 1, "serialization"), rec(0, 2, "connector")])        # replayed
    return {"by_category": dict(await rows("SELECT key, sum(count) FROM dc_failure_counts WHERE kind = 'ingestion' GROUP BY key"))}
''')
    assert {k: int(v) for k, v in result["by_category"].items()} == {"serialization": 2, "connector": 1}


# ── Atomicity: the counts and the offsets are one transaction ──────────────────────────────────────────────────────────

def test_if_the_counts_cannot_be_written_the_offsets_are_not_advanced_and_nothing_is_lost(db) -> None:
    result = _run(db, '''
async def main():
    batch = [rec(0, o) for o in range(5)]
    real = dlq.add_ingestion_counts

    async def broken(*a, **kw):
        raise RuntimeError("the database went away mid-batch")

    dlq.add_ingestion_counts = broken
    try:
        await once(batch)
        failed = False
    except RuntimeError:
        failed = True
    dlq.add_ingestion_counts = real
    between = {"total": await total(), "marks": await watermarks()}        # neither the counts NOR the offsets were written
    retried = await once(batch)                                             # the redelivered batch is counted in full
    return {"failed": failed, "between": between, "retried": [retried.counted, retried.replayed], "total": await total(), "marks": await watermarks()}
''')
    assert result["failed"] is True
    assert result["between"] == {"total": 0, "marks": {}}
    assert result["retried"] == [5, 0]
    assert result["total"] == 5
    assert result["marks"] == {"0": 5}


def test_a_failure_after_the_counts_but_before_the_commit_rolls_both_back(db) -> None:
    result = _run(db, '''
async def main():
    batch = [rec(0, o) for o in range(5)]
    real = dlq.FailureCountOffsetRepository.advance

    async def broken(self, **kw):
        raise RuntimeError("died while moving the offsets")

    dlq.FailureCountOffsetRepository.advance = broken
    try:
        await once(batch)
    except RuntimeError:
        pass
    dlq.FailureCountOffsetRepository.advance = real
    return {"total": await total(), "marks": await watermarks()}          # the increments are NOT left behind without their offsets
''')
    assert result == {"total": 0, "marks": {}}


# ── Two readers at once ────────────────────────────────────────────────────────────────────────────────────────────────

def test_two_replicas_handed_the_same_records_at_the_same_time_count_them_once(db) -> None:
    result = _run(db, '''
async def main():
    batch = [rec(0, o) for o in range(50)]
    outcomes = await asyncio.gather(*[once(batch) for _ in range(6)])         # a rebalance hands one partition to several readers
    return {"counted": sorted(o.counted for o in outcomes), "total": await total(), "marks": await watermarks()}
''')
    assert result["counted"] == [0, 0, 0, 0, 0, 50]                          # exactly one of them counted anything
    assert result["total"] == 50
    assert result["marks"] == {"0": 50}


# ── What is and is not shared ──────────────────────────────────────────────────────────────────────────────────────────

def test_the_offsets_belong_to_the_topic_not_to_a_pipeline(db) -> None:
    """One topic, one progress. Deleting a pipeline must not wipe it: the topic's other pipelines would be counted all over again."""
    result = _run(db, '''
async def main():
    await once([rec(0, o) for o in range(7)])
    async with AsyncSessionLocal() as s:
        await s.execute(text(f"DELETE FROM dc_pipelines WHERE id = '{PIPE_A}'"))
        await s.commit()
    return {"marks": await watermarks(), "counts_of_the_deleted_pipeline": await total(PIPE_A)}
''')
    assert result == {"marks": {"0": 7}, "counts_of_the_deleted_pipeline": 0}           # its counts went with it; the topic's progress did not


# ── A topic that was deleted and recreated ─────────────────────────────────────────────────────────────────────────────

def test_offsets_beyond_a_recreated_topics_end_are_forgotten_so_new_records_are_not_skipped(db) -> None:
    result = _run(db, '''
async def main():
    await once([rec(0, o) for o in range(100)] + [rec(1, o) for o in range(3)])
    # the topic is deleted and recreated: it starts again at 0 and now ends at 5 on partition 0, and at 10 on partition 1
    forgotten = await dlq.forget_offsets_past_topic_end(topic=TOPIC, end_offsets={0: 5, 1: 10})
    marks = await watermarks()
    fresh = await once([rec(0, o) for o in range(5)])
    return {"forgotten": forgotten, "marks_after_forget": marks, "fresh": [fresh.counted, fresh.replayed], "total": await total()}
''')
    assert result["forgotten"] == [0]                                        # partition 0 (100 > 5), not partition 1 (3 <= 10)
    assert result["marks_after_forget"] == {"1": 3}
    assert result["fresh"] == [5, 0]                                         # the recreated topic's records are counted
    assert result["total"] == 108


def test_offsets_within_the_topic_are_kept(db) -> None:
    result = _run(db, '''
async def main():
    await once([rec(0, o) for o in range(10)])
    forgotten = await dlq.forget_offsets_past_topic_end(topic=TOPIC, end_offsets={0: 10})   # caught up exactly
    forgotten_2 = await dlq.forget_offsets_past_topic_end(topic=TOPIC, end_offsets={0: 50, 7: 1})
    return {"forgotten": forgotten, "forgotten_2": forgotten_2, "marks": await watermarks()}
''')
    assert result == {"forgotten": [], "forgotten_2": [], "marks": {"0": 10}}


# ── The worker's own ordering, with a consumer that "crashes" on its Kafka commit ──────────────────────────────────────

def test_a_worker_whose_process_dies_before_the_kafka_commit_does_not_double_count_after_restart(db) -> None:
    result = _run(db, '''
class FakeConsumer:
    """Serves the same ten records on the first poll, and "dies" when asked to commit its offset (if told to)."""
    def __init__(self, die_on_commit):
        self.die_on_commit, self.served, self.commits, self.closed = die_on_commit, False, 0, False
    def poll(self, timeout_ms, max_records):
        time.sleep(0.01)
        if self.served:
            return {}
        self.served = True
        return {("tp", 0): [rec(0, o) for o in range(10)]}
    def commit(self):
        if self.die_on_commit:
            raise RuntimeError("process died before the Kafka commit")
        self.commits += 1
    def close(self):
        self.closed = True
    def partitions_for_topic(self, topic):
        return {0}
    def end_offsets(self, partitions):
        return {tp: 10 for tp in partitions}

async def main():
    first = FakeConsumer(die_on_commit=True)
    dlq._open_consumer = lambda topic, group: first
    worker = dlq.DlqCounterWorker(org_id="org_a", pipeline_id=PIPE_A, topic=TOPIC, attribution=ATTR_A)
    died = False
    try:
        await worker._run()
    except RuntimeError:
        died = True
    after_crash = await total()                                   # the database commit happened before the death

    second = FakeConsumer(die_on_commit=False)                    # the restarted consumer is redelivered the same records
    dlq._open_consumer = lambda topic, group: second
    worker2 = dlq.DlqCounterWorker(org_id="org_a", pipeline_id=PIPE_A, topic=TOPIC, attribution=ATTR_A)
    task = asyncio.create_task(worker2._run())
    for _ in range(300):
        if second.commits:
            break
        await asyncio.sleep(0.02)
    worker2._stopping = True
    await asyncio.wait_for(task, 10)
    return {"died": died, "after_crash": after_crash, "after_restart": await total(), "kafka_commits": second.commits, "marks": await watermarks()}
''')
    assert result["died"] is True
    assert result["after_crash"] == 10
    assert result["after_restart"] == 10                                   # the replay was not counted
    assert result["kafka_commits"] == 1                                    # and its Kafka offset is committed now
    assert result["marks"] == {"0": 10}


def test_the_worker_commits_the_database_before_it_commits_kafka(db) -> None:
    result = _run(db, '''
class FakeConsumer:
    def __init__(self):
        self.served, self.db_total_at_kafka_commit, self.commits = False, None, 0
    def poll(self, timeout_ms, max_records):
        time.sleep(0.01)
        if self.served:
            return {}
        self.served = True
        return {("tp", 0): [rec(0, o) for o in range(4)]}
    def commit(self):
        self.commits += 1
        self.db_total_at_kafka_commit = asyncio.run_coroutine_threadsafe(total(), loop).result(20)
    def close(self): pass
    def partitions_for_topic(self, topic): return {0}
    def end_offsets(self, partitions): return {tp: 4 for tp in partitions}

async def main():
    global loop
    loop = asyncio.get_running_loop()
    consumer = FakeConsumer()
    dlq._open_consumer = lambda topic, group: consumer
    worker = dlq.DlqCounterWorker(org_id="org_a", pipeline_id=PIPE_A, topic=TOPIC, attribution=ATTR_A)
    task = asyncio.create_task(worker._run())
    for _ in range(300):
        if consumer.commits:
            break
        await asyncio.sleep(0.02)
    worker._stopping = True
    await asyncio.wait_for(task, 10)
    return {"seen_by_kafka_commit": consumer.db_total_at_kafka_commit}
''')
    assert result["seen_by_kafka_commit"] == 4          # the counts were already committed when the Kafka offset was


def test_a_worker_started_on_a_recreated_topic_forgets_the_old_offsets_and_counts_the_new_records(db) -> None:
    result = _run(db, '''
class FakeConsumer:
    def __init__(self):
        self.served, self.commits = False, 0
    def poll(self, timeout_ms, max_records):
        time.sleep(0.01)
        if self.served:
            return {}
        self.served = True
        return {("tp", 0): [rec(0, o) for o in range(5)]}              # the recreated topic's first five records
    def commit(self): self.commits += 1
    def close(self): pass
    def partitions_for_topic(self, topic): return {0}
    def end_offsets(self, partitions): return {tp: 5 for tp in partitions}   # it now ends at 5 ...

async def main():
    await once([rec(0, o) for o in range(100)])                         # ... but the database remembers having counted up to 100
    before = await total()
    consumer = FakeConsumer()
    dlq._open_consumer = lambda topic, group: consumer
    worker = dlq.DlqCounterWorker(org_id="org_a", pipeline_id=PIPE_A, topic=TOPIC, attribution=ATTR_A)
    task = asyncio.create_task(worker._run())
    for _ in range(300):
        if consumer.commits:
            break
        await asyncio.sleep(0.02)
    worker._stopping = True
    await asyncio.wait_for(task, 10)
    return {"before": before, "after": await total(), "marks": await watermarks()}
''')
    assert result == {"before": 100, "after": 105, "marks": {"0": 5}}            # the five new records were not skipped


def test_concurrent_readers_count_new_records_once_when_the_offset_row_already_exists(db) -> None:
    """End to end, eight readers. (This alone cannot prove the lock — each reader must first open its own connection, so they tend to run one
    after another; the deterministic test below holds the lock open and proves the second reader has to wait.)"""
    result = _run(db, '''
async def main():
    await once([rec(0, o) for o in range(10)])                                 # the offset row now exists
    batch = [rec(0, o) for o in range(60)]                                     # 10..59 are new
    outcomes = await asyncio.gather(*[once(batch) for _ in range(8)])
    return {"counted": sorted(o.counted for o in outcomes), "total": await total(), "marks": await watermarks()}
''')
    assert result["counted"] == [0, 0, 0, 0, 0, 0, 0, 50]
    assert result["total"] == 60
    assert result["marks"] == {"0": 60}


def test_a_second_reader_must_wait_for_the_first_to_commit_and_then_sees_its_offsets(db) -> None:
    """The lock itself, deterministically: reader A has locked the partition's offset and is mid-batch; reader B asking for the same partition
    must BLOCK, and when A commits it sees A's new offset — so it skips what A counted instead of counting it again."""
    result = _run(db, '''
from app.repositories.dataconnect import FailureCountOffsetRepository as Repo

async def main():
    await once([rec(0, o) for o in range(10)])                              # the offset row exists (a plain insert would not block)
    async with AsyncSessionLocal() as sa, AsyncSessionLocal() as sb:
        a, b = Repo(sa), Repo(sb)
        a_sees = await a.lock(org_id="org_a", kind="ingestion", topic=TOPIC, partitions=[0])
        task_b = asyncio.create_task(b.lock(org_id="org_a", kind="ingestion", topic=TOPIC, partitions=[0]))
        await asyncio.sleep(0.7)
        blocked = not task_b.done()                                         # B is waiting for A
        await a.advance(org_id="org_a", kind="ingestion", topic=TOPIC, next_offsets={0: 20})
        await sa.commit()
        b_sees = await asyncio.wait_for(task_b, 10)
        await sb.rollback()
    return {"a_sees": a_sees, "blocked": blocked, "b_sees": b_sees}
''')
    assert result["a_sees"] == {"0": 10}
    assert result["blocked"] is True                       # without the row lock this is False: B reads the stale 10 straight away
    assert result["b_sees"] == {"0": 20}                   # and once A has committed, B skips everything A counted
