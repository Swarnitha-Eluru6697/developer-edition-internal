"""
NEW-05, against a REAL PostgreSQL: the quality worker's statistics are counted exactly once per source record, even when the worker is handed the same
record again after a crash between recording it and committing its Kafka offset.

Before, a replay counted the record a second time: total and failed counts, the failure sample and the per-rule counts on the Quality page were all
inflated. The claim on (pipeline, topic, partition, offset) is now written in the same transaction as the statistics.

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
POLICY_ID = "00000000-0000-0000-0000-0000000000d1"
TOPIC = "raw-ext-feed-alpha-batch-v1"

PRELUDE = '''
import asyncio, json
from types import SimpleNamespace
from sqlalchemy import text
from app.db.session import AsyncSessionLocal
from app.repositories.dataconnect import QualityStreamOffsetRepository as Offsets
from app.services import quality_stream_worker as qsw
from app.services.kafka_runtime import InMemoryKafkaProducer, KafkaRecord
from app.services.quality_stream_worker import QualityStreamWorker, _Origin

PIPE_A, PIPE_B, POLICY_ID, TOPIC = "@A@", "@B@", "@POLICY@", "@TOPIC@"
POLICY = {"id": POLICY_ID, "on_failure": "route_to_quarantine", "quarantine_sink": "kafka",
          "rules": [{"name": "stock_range", "type": "min_max_numeric", "field": "stock_quantity", "min": 20, "max": 150}]}

def pipeline(pid, **over):
    return {"id": pid, "org_id": "org_a", "direction": "SINK", "zone": "raw", "domain": "d", "source_system": "s", "entity": "e", "mode": "batch",
            "version": "v1", "topic_name": None, "schema_version_id": None, "connector_config": {"topics": TOPIC},
            "quality_policy_id": POLICY_ID, "quality_mode": "OBSERVE", **over}

def worker(pid=PIPE_A, **over):
    return QualityStreamWorker(org_id="org_a", pipeline=pipeline(pid, **over), policy=POLICY)

async def rows(sql):
    async with AsyncSessionLocal() as s:
        return [tuple(r) for r in (await s.execute(text(sql))).all()]

async def stats(pid=PIPE_A):
    r = await rows(f"SELECT total_count, failed_count, jsonb_array_length(sample_failures) FROM dc_quality_stream_stats WHERE pipeline_id = '{pid}'")
    return list(r[0]) if r else [0, 0, 0]

async def rule_counts(pid=PIPE_A):
    return {k: int(n) for k, n in await rows(f"SELECT key, sum(count) FROM dc_failure_counts WHERE kind = 'validation' AND pipeline_id = '{pid}' GROUP BY 1")}

async def marks(pid=PIPE_A):
    return {f"{t}:{p}": n for t, p, n in await rows(f"SELECT topic, kafka_partition, next_offset FROM dc_quality_stream_offsets WHERE pipeline_id = '{pid}' ORDER BY 1, 2")}

def origin(offset, partition=0, topic=TOPIC):
    return _Origin(topic=topic, partition=partition, offset=offset)

async def record(w, offset, valid=True, partition=0, topic=TOPIC, rules=("stock_range",)):
    return await w._record_outcome(valid=valid, failed_rules=[] if valid else list(rules), original_record={"id": offset, "stock_quantity": 50 if valid else 1},
                                   origin=origin(offset, partition, topic))
'''.replace("@A@", PIPE_A).replace("@B@", PIPE_B).replace("@POLICY@", POLICY_ID).replace("@TOPIC@", TOPIC)


def _pipeline_sql(pid: str, name: str, mode: str) -> str:
    return f"""
        INSERT INTO dc_pipelines (id, org_id, name, template_id, connection_profile_id, zone, domain, source_system, entity, mode, version, direction,
            connector_name, connector_config, generated_config, template_snapshot, status, error_handling_policy, runtime_pool_id, quality_policy_id)
          VALUES ('{pid}', 'org_a', '{name}', 'tpl_a', '00000000-0000-0000-0000-0000000000a1', 'raw', 'd', 's', '{name}', '{mode}', 'v1',
                  'SINK', 'd--s--{name}--{mode}--v1', '{{"topics": "{TOPIC}"}}', '{{}}', '{{}}', 'running', 'DLQ_AND_CONTINUE', 'connect-sink-pool', '{POLICY_ID}');
    """


@pytest.fixture
def db():
    with scratch_database("dc_qoffsets") as scratch:
        assert scratch.startup_migrations().returncode == 0
        scratch.execute(f"""
            INSERT INTO dc_organizations (org_id, name, status) VALUES ('org_a', 'Org A', 'active');
            INSERT INTO dc_connector_templates (id, name, connector_type, category, connector_class, runtime_pool_type, config_schema, default_config,
                default_transforms, version, is_active, supported_transforms, supported_kafka_connect_versions, supported_source_versions,
                transform_compatibility, supported_modes, supports_dlq, supports_schema_discovery)
              VALUES ('tpl_a', 'T', 'sink', 'database', 'x.Y', 'connect-sink-pool', '{{}}', '{{}}', '[]', '1', true, '[]', '[]', '[]', '{{}}', '[]', true, false);
            INSERT INTO dc_connection_profiles (id, org_id, name, source_type, connection_config)
              VALUES ('00000000-0000-0000-0000-0000000000a1', 'org_a', 'conn', 'postgresql', '{{}}');
            INSERT INTO dc_quality_policies (id, org_id, name, rules, on_failure, quarantine_sink)
              VALUES ('{POLICY_ID}', 'org_a', 'p', '[]', 'route_to_quarantine', 'kafka');
        """)
        scratch.execute(_pipeline_sql(PIPE_A, "a", "batch"))
        scratch.execute(_pipeline_sql(PIPE_B, "b", "stream"))
        yield scratch


def _run(scratch, body: str, timeout: int = 120):
    code = PRELUDE + "\n" + body + "\nprint('RESULT=' + json.dumps(asyncio.run(main()), default=str))\n"
    result = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, env=scratch.env(), capture_output=True, text=True, timeout=timeout)
    assert result.returncode == 0, result.stderr[-2500:]
    line = next(ln for ln in reversed(result.stdout.splitlines()) if ln.startswith("RESULT="))
    return json.loads(line[len("RESULT="):])


# ── The review's scenario ──────────────────────────────────────────────────────────────────────────────────────────────

def test_a_replayed_batch_does_not_change_the_statistics(db) -> None:
    """Ten records, three invalid, recorded; the process dies before the Kafka commit; the same ten arrive again."""
    result = _run(db, '''
async def main():
    w = worker()
    valid = [True, True, False, True, True, False, True, True, True, False]            # offsets 2, 5, 9 fail
    first = [await record(w, o, v) for o, v in enumerate(valid)]
    after_first = {"stats": await stats(), "rules": await rule_counts(), "marks": await marks()}
    replay = [await record(w, o, v) for o, v in enumerate(valid)]                      # <- the redelivery
    return {"first": first, "replay": replay, "after_first": after_first,
            "after_replay": {"stats": await stats(), "rules": await rule_counts(), "marks": await marks()}}
''')
    assert result["first"] == [True] * 10                              # every record counted the first time
    assert result["replay"] == [False] * 10                            # and recognised as already counted the second
    assert result["after_first"]["stats"] == [10, 3, 3]                # total, failed, failure samples
    assert result["after_first"]["rules"] == {"*": 3, "stock_range": 3}
    assert result["after_replay"] == result["after_first"]            # nothing moved: totals, sample, per-rule counts, offsets
    assert result["after_replay"]["marks"] == {f"{TOPIC}:0": 10}


def test_a_replay_that_overlaps_new_records_counts_only_the_new_ones(db) -> None:
    result = _run(db, '''
async def main():
    w = worker()
    for o in range(6):
        await record(w, o, o != 3)                                                     # offset 3 fails
    outcomes = [await record(w, o, o != 3) for o in range(4, 10)]                      # 4, 5 again; 6..9 are new
    return {"outcomes": outcomes, "stats": await stats()}
''')
    assert result["outcomes"] == [False, False, True, True, True, True]
    assert result["stats"] == [10, 1, 1]


def test_each_partition_and_each_topic_has_its_own_position(db) -> None:
    result = _run(db, '''
async def main():
    w = worker()
    a = await record(w, 7, True, partition=0)
    b = await record(w, 7, True, partition=1)                                          # same offset, another partition
    c = await record(w, 7, True, partition=0, topic="raw-ext-feed-beta-batch-v1")  # same offset and partition, another topic
    again = await record(w, 7, True, partition=1)
    return {"flags": [a, b, c, again], "stats": await stats(), "marks": await marks()}
''')
    assert result["flags"] == [True, True, True, False]
    assert result["stats"] == [3, 0, 0]
    assert result["marks"] == {f"{TOPIC}:0": 8, f"{TOPIC}:1": 8, "raw-ext-feed-beta-batch-v1:0": 8}


def test_offset_zero_is_a_real_offset(db) -> None:
    result = _run(db, '''
async def main():
    w = worker()
    return {"flags": [await record(w, 0), await record(w, 0)], "stats": await stats()}
''')
    assert result == {"flags": [True, False], "stats": [1, 0, 0]}


def test_a_lower_offset_after_a_higher_one_is_a_replay_and_the_position_never_moves_back(db) -> None:
    result = _run(db, '''
async def main():
    w = worker()
    await record(w, 9)
    late = await record(w, 3)                                                          # an old record redelivered on its own
    return {"late": late, "stats": await stats(), "marks": await marks()}
''')
    assert result == {"late": False, "stats": [1, 0, 0], "marks": {f"{TOPIC}:0": 10}}


def test_a_record_with_no_kafka_position_is_always_counted(db) -> None:
    """Handed to the worker directly, it cannot be recognised as a repeat, so it is never skipped."""
    result = _run(db, '''
async def main():
    w = worker()
    for _ in range(2):
        await w._record_outcome(valid=True, failed_rules=[], original_record={"id": 1}, origin=None)
    await w._record_outcome(valid=True, failed_rules=[], original_record={"id": 1}, origin=_Origin(topic=TOPIC))
    return {"stats": await stats(), "marks": await marks()}
''')
    assert result == {"stats": [3, 0, 0], "marks": {}}


def test_two_pipelines_reading_the_same_topic_each_count_the_record_for_themselves(db) -> None:
    result = _run(db, '''
async def main():
    a, b = worker(PIPE_A), worker(PIPE_B)
    first = [await record(a, 0, False), await record(b, 0, False)]
    replay = [await record(a, 0, False), await record(b, 0, False)]
    return {"first": first, "replay": replay, "a": await stats(PIPE_A), "b": await stats(PIPE_B)}
''')
    assert result == {"first": [True, True], "replay": [False, False], "a": [1, 1, 1], "b": [1, 1, 1]}


# ── Atomicity ──────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_if_the_statistics_cannot_be_written_the_record_is_not_claimed_and_is_counted_when_retried(db) -> None:
    result = _run(db, '''
async def main():
    w = worker()
    real = qsw.record_validation_failure
    async def broken(*a, **kw):
        raise RuntimeError("the database went away mid-record")
    qsw.record_validation_failure = broken
    try:
        await record(w, 0, False)
        failed = False
    except RuntimeError:
        failed = True
    qsw.record_validation_failure = real
    between = {"stats": await stats(), "marks": await marks()}           # neither the totals NOR the claim were written
    retried = await record(w, 0, False)                                    # so the retry counts it, once
    return {"failed": failed, "between": between, "retried": retried, "stats": await stats(), "rules": await rule_counts()}
''')
    assert result["failed"] is True
    assert result["between"] == {"stats": [0, 0, 0], "marks": {}}
    assert result["retried"] is True
    assert result["stats"] == [1, 1, 1]
    assert result["rules"] == {"*": 1, "stock_range": 1}


# ── Two writers ────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_a_second_writer_must_wait_for_the_first_to_commit_and_then_sees_it_was_counted(db) -> None:
    """The claim, deterministically: writer A has claimed an offset and not yet committed; writer B claiming the SAME offset must block, and
    once A commits it is told the record was already counted."""
    result = _run(db, '''
async def main():
    async with AsyncSessionLocal() as sa, AsyncSessionLocal() as sb:
        await Offsets(sa).claim(pipeline_id=PIPE_A, topic=TOPIC, partition=0, offset=0)      # the row exists, committed
        await sa.commit()
        a_got = await Offsets(sa).claim(pipeline_id=PIPE_A, topic=TOPIC, partition=0, offset=5)
        task_b = asyncio.create_task(Offsets(sb).claim(pipeline_id=PIPE_A, topic=TOPIC, partition=0, offset=5))
        await asyncio.sleep(0.7)
        blocked = not task_b.done()
        await sa.commit()
        b_got = await asyncio.wait_for(task_b, 10)
        await sb.rollback()
    return {"a_got": a_got, "blocked": blocked, "b_got": b_got}
''')
    assert result == {"a_got": True, "blocked": True, "b_got": False}


# ── Lifecycle ──────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_a_deleted_pipeline_takes_its_offsets_with_it(db) -> None:
    result = _run(db, '''
async def main():
    await record(worker(PIPE_A), 3)
    await record(worker(PIPE_B), 3)
    async with AsyncSessionLocal() as s:
        await s.execute(text(f"DELETE FROM dc_pipelines WHERE id = '{PIPE_A}'"))
        await s.commit()
    return {"a": await marks(PIPE_A), "b": await marks(PIPE_B)}
''')
    assert result == {"a": {}, "b": {f"{TOPIC}:0": 4}}


def test_a_worker_started_on_a_recreated_topic_forgets_the_old_offsets_and_counts_the_new_records(db) -> None:
    result = _run(db, '''
class FakeConsumer:
    def __init__(self):
        self.served, self.commits = False, 0
    async def poll(self):
        if self.served:
            await asyncio.sleep(0.01)
            return None
        self.served = True
        return KafkaRecord(topic=TOPIC, key="k", partition=0, offset=0, value={"_raw": json.dumps({"schema": {}, "payload": {"stock_quantity": 50}})})
    async def commit(self): self.commits += 1
    async def close(self): pass
    async def end_offsets(self): return {(TOPIC, 0): 5}          # the topic now ends at 5 ...

async def main():
    for o in range(100):
        await record(worker(), o)                                 # ... but the database remembers having counted up to 100
    before = await stats()
    consumer = FakeConsumer()
    qsw.NativeKafkaConsumer = lambda **kw: consumer
    qsw.get_kafka_producer = lambda: asyncio.sleep(0, InMemoryKafkaProducer())
    w = worker()
    task = asyncio.create_task(w._run())
    for _ in range(300):
        if consumer.commits:
            break
        await asyncio.sleep(0.02)
    w._stopping = True
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    return {"before": before, "after": await stats(), "marks": await marks()}
''')
    assert result["before"] == [100, 0, 0]
    assert result["after"] == [101, 0, 0]                          # the new topic's first record was counted, not skipped as "already seen"
    assert result["marks"] == {f"{TOPIC}:0": 1}


def test_a_failed_recreated_topic_check_does_not_stop_the_worker(db) -> None:
    result = _run(db, '''
class FakeConsumer:
    def __init__(self):
        self.served, self.commits = False, 0
    async def poll(self):
        if self.served:
            await asyncio.sleep(0.01)
            return None
        self.served = True
        return KafkaRecord(topic=TOPIC, key="k", partition=0, offset=0, value={"_raw": json.dumps({"schema": {}, "payload": {"stock_quantity": 50}})})
    async def commit(self): self.commits += 1
    async def close(self): pass
    async def end_offsets(self): raise RuntimeError("broker metadata unavailable")

async def main():
    consumer = FakeConsumer()
    qsw.NativeKafkaConsumer = lambda **kw: consumer
    w = worker()
    task = asyncio.create_task(w._run())
    for _ in range(300):
        if consumer.commits:
            break
        await asyncio.sleep(0.02)
    w._stopping = True
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    return {"commits": consumer.commits, "stats": await stats()}
''')
    assert result == {"commits": 1, "stats": [1, 0, 0]}


# ── The worker's own loop, with a real database and a crash on the Kafka commit ────────────────────────────────────────

def test_a_crash_after_publishing_but_before_the_kafka_commit_leaves_the_statistics_stable_and_the_repeats_identifiable(db) -> None:
    result = _run(db, '''
STOCK = [50, 60, 5, 70, 80, 200, 90, 100, 110, 1]                    # three invalid
def records():
    return [KafkaRecord(topic=TOPIC, key=f"k{o}", partition=0, offset=o,
                        value={"_raw": json.dumps({"schema": {"type": "struct", "fields": []}, "payload": {"id": o, "stock_quantity": s}})})
            for o, s in enumerate(STOCK)]

class FakeConsumer:
    def __init__(self, die_on_commit=None):
        self.queue, self.commits, self.die = records(), 0, die_on_commit
    async def poll(self):
        if self.queue:
            return self.queue.pop(0)
        await asyncio.sleep(0.01)
        return None
    async def commit(self):
        self.commits += 1
        if self.die == self.commits:
            raise RuntimeError("the process died before the Kafka commit")
    async def close(self): pass

kafka = InMemoryKafkaProducer()                                       # Kafka outlives the worker "process"
async def _producer(): return kafka
async def _ensure(topic, **kw): return None
qsw.get_kafka_producer, qsw.ensure_simple_topic = _producer, _ensure

async def run_worker(die_on_commit=None):
    consumer = FakeConsumer(die_on_commit)
    qsw.NativeKafkaConsumer = lambda **kw: consumer
    w = worker(quality_mode="ENFORCE")
    task = asyncio.create_task(w._run())
    for _ in range(400):
        if consumer.commits >= 10 or task.done():
            break
        await asyncio.sleep(0.01)
    w._stopping = True
    if not task.done():
        task.cancel()
    try:
        await task
    except (asyncio.CancelledError, RuntimeError):
        pass

async def main():
    await run_worker(die_on_commit=10)                                # everything recorded and published; dies on the LAST commit
    after_first = {"stats": await stats(), "rules": await rule_counts(), "published": len(kafka.records)}
    await run_worker()                                                # the restart: all ten delivered again
    by_topic = {}
    for r in kafka.records:
        by_topic.setdefault(r.topic.split("-")[1], []).append(r.headers["inai.dedupe_key"])
    return {"after_first": after_first, "after_restart": {"stats": await stats(), "rules": await rule_counts(), "published": len(kafka.records)},
            "keys": {zone: [len(keys), len(set(keys))] for zone, keys in by_topic.items()}}
''')
    assert result["after_first"] == {"stats": [10, 3, 3], "rules": {"*": 3, "stock_range": 3}, "published": 10}
    assert result["after_restart"]["stats"] == [10, 3, 3]              # stable: the replay was not counted
    assert result["after_restart"]["rules"] == {"*": 3, "stock_range": 3}
    assert result["after_restart"]["published"] == 20                  # re-published: at-least-once ...
    assert result["keys"] == {"quarantine": [6, 3], "prepared": [14, 7]}  # ... but every repeat has the same dedupe key as its original


def test_a_worker_exactly_caught_up_with_its_topic_keeps_its_offsets(db) -> None:
    """Stored offset == the topic's end means "everything has been counted", NOT "the topic was recreated": it must be kept, or the
    next replay would be counted again."""
    result = _run(db, '''
class FakeConsumer:
    def __init__(self): self.commits = 0
    async def poll(self):
        await asyncio.sleep(0.01)
        return None
    async def commit(self): self.commits += 1
    async def close(self): pass
    async def end_offsets(self): return {(TOPIC, 0): 10}          # 10 records exist, all counted

async def main():
    for o in range(10):
        await record(worker(), o)
    consumer = FakeConsumer()
    qsw.NativeKafkaConsumer = lambda **kw: consumer
    w = worker()
    task = asyncio.create_task(w._run())
    await asyncio.sleep(0.8)
    w._stopping = True
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    return {"marks": await marks(), "replay": await record(worker(), 9)}
''')
    assert result == {"marks": {f"{TOPIC}:0": 10}, "replay": False}
