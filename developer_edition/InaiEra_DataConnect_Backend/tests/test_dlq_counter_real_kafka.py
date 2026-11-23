"""
NEW-03, against a REAL Kafka broker and a REAL PostgreSQL: crash after the database commit but before the Kafka commit, then replay — the counter
must not increase on the replay.

The real DlqCounterWorker and the real kafka-python consumer; only the crash is injected (the consumer's commit() raises, standing in for the
process dying between the two commits). A scratch database and a throwaway topic are used and removed afterwards.

Skipped unless BOTH DATACONNECT_REAL_DB_TESTS=1 and DATACONNECT_REAL_KAFKA_TESTS=1 (the broker is taken from DATACONNECT_KAFKA_BOOTSTRAP,
default localhost:9092).
"""

import json
import os
import subprocess
import sys
import uuid

import pytest
from scratch_db import BACKEND, scratch_database

pytestmark = pytest.mark.skipif(
    not (os.environ.get("DATACONNECT_REAL_DB_TESTS") and os.environ.get("DATACONNECT_REAL_KAFKA_TESTS")),
    reason="needs a real PostgreSQL and Kafka (DATACONNECT_REAL_DB_TESTS=1 DATACONNECT_REAL_KAFKA_TESTS=1)",
)

PIPE = "00000000-0000-0000-0000-0000000000a2"

SCENARIO = '''
import asyncio, json, sys, time
from sqlalchemy import text
from kafka import KafkaConsumer, KafkaProducer
from kafka.admin import KafkaAdminClient, NewTopic
from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.services import dlq_failure_counter as dlq
from app.services.dlq_attribution import DlqAttribution

PIPE = "%(pipe)s"
TOPIC = "%(topic)s"
GROUP = f"inai-dlqcount-org_a-{PIPE}"
ATTR = DlqAttribution([{"id": PIPE, "connector_name": "c-e"}])
BOOTSTRAP = settings.kafka_bootstrap
HEADERS = [
    ("__connect.errors.stage", b"VALUE_CONVERTER"),
    ("__connect.errors.exception.class.name", b"org.apache.kafka.connect.errors.DataException"),
    ("__connect.errors.exception.message", b"Converting byte[] to Kafka Connect data failed due to serialization error"),
]

async def total():
    async with AsyncSessionLocal() as s:
        return int((await s.execute(text("SELECT coalesce(sum(count), 0) FROM dc_failure_counts WHERE kind = 'ingestion'"))).scalar())

def committed_total(admin):
    """Sum of the group's committed offsets, read through a plain consumer (it does not join the group), so it works on any kafka-python."""
    from kafka import TopicPartition
    probe = KafkaConsumer(bootstrap_servers=BOOTSTRAP, group_id=GROUP, enable_auto_commit=False)
    try:
        return sum((probe.committed(TopicPartition(TOPIC, p)) or 0) for p in (0, 1))
    finally:
        probe.close()

def produce(n, start):
    producer = KafkaProducer(bootstrap_servers=BOOTSTRAP)
    for i in range(n):
        producer.send(TOPIC, value=f"dead-{start + i}".encode(), headers=HEADERS, partition=(start + i) %% 2)
    producer.flush(); producer.close()

async def wait_for(predicate, seconds, what):
    for _ in range(int(seconds / 0.25)):
        if predicate():
            return True
        await asyncio.sleep(0.25)
    return False

async def main():
    admin = KafkaAdminClient(bootstrap_servers=BOOTSTRAP)
    out = {}
    try:
        admin.create_topics([NewTopic(TOPIC, num_partitions=2, replication_factor=1)])
        time.sleep(2)
        produce(10, 0)

        # ── Worker 1: counts the ten records, commits them to PostgreSQL, then "the process dies" on the Kafka commit. ──
        real_commit = KafkaConsumer.commit
        def crash(self, *a, **kw):
            raise RuntimeError("the process died before committing the Kafka offset")
        KafkaConsumer.commit = crash
        worker1 = dlq.DlqCounterWorker(org_id="org_a", pipeline_id=PIPE, topic=TOPIC, attribution=ATTR)
        try:
            await asyncio.wait_for(worker1._run(), 90)
            out["crashed"] = False
        except RuntimeError:
            out["crashed"] = True
        finally:
            KafkaConsumer.commit = real_commit
        out["after_crash_db"] = await total()
        out["after_crash_kafka_committed"] = committed_total(admin)          # nothing was committed to Kafka

        # ── Worker 2 = the restarted process: Kafka has no committed offset, so the group starts from the beginning and the SAME ten
        #    records arrive again. ──
        worker2 = dlq.DlqCounterWorker(org_id="org_a", pipeline_id=PIPE, topic=TOPIC, attribution=ATTR)
        worker2.start()
        committed = await wait_for(lambda: committed_total(admin) >= 10, 90, "kafka commit after replay")
        out["replay_reached_kafka_commit"] = committed
        out["after_replay_db"] = await total()                                # <- must still be 10

        # ── New records after the replay are still counted (the filter does not over-skip). ──
        produce(5, 10)
        async def grown():
            return await total() >= 15
        for _ in range(240):
            if await grown():
                break
            await asyncio.sleep(0.25)
        out["after_new_records_db"] = await total()
        await worker2.stop()
        async with AsyncSessionLocal() as s:
            out["marks"] = {int(p): int(n) for p, n in (await s.execute(text("SELECT kafka_partition, next_offset FROM dc_failure_count_offsets ORDER BY 1"))).all()}
    finally:
        try: admin.delete_topics([TOPIC])
        except Exception: pass
        for name in ("delete_groups", "delete_consumer_groups"):
            try:
                getattr(admin, name)([GROUP]); break
            except Exception: pass
        admin.close()
    return out
'''


def _seed(db) -> None:
    db.execute(f"""
        INSERT INTO dc_organizations (org_id, name, status) VALUES ('org_a', 'Org A', 'active');
        INSERT INTO dc_connector_templates (id, name, connector_type, category, connector_class, runtime_pool_type, config_schema, default_config,
            default_transforms, version, is_active, supported_transforms, supported_kafka_connect_versions, supported_source_versions,
            transform_compatibility, supported_modes, supports_dlq, supports_schema_discovery)
          VALUES ('tpl_a', 'T', 'sink', 'database', 'x.Y', 'connect-sink-pool', '{{}}', '{{}}', '[]', '1', true, '[]', '[]', '[]', '{{}}', '[]', true, false);
        INSERT INTO dc_connection_profiles (id, org_id, name, source_type, connection_config)
          VALUES ('00000000-0000-0000-0000-0000000000a1', 'org_a', 'conn', 'postgresql', '{{}}');
        INSERT INTO dc_pipelines (id, org_id, name, template_id, connection_profile_id, zone, domain, source_system, entity, mode, version, direction,
            connector_name, connector_config, generated_config, template_snapshot, status, error_handling_policy, runtime_pool_id)
          VALUES ('{PIPE}', 'org_a', 'pipe', 'tpl_a', '00000000-0000-0000-0000-0000000000a1', 'raw', 'd', 's', 'e', 'batch', 'v1',
                  'SINK', 'c-e', '{{"topics": "raw-d-s-in-batch-v1"}}', '{{}}', '{{}}', 'running', 'DLQ_AND_CONTINUE', 'connect-sink-pool');
    """)


def test_crash_after_the_database_commit_but_before_the_kafka_commit_then_replay_does_not_increase_the_counter() -> None:
    topic = f"dlqtest-{uuid.uuid4().hex[:10]}"
    with scratch_database("dc_dlqkafka") as db:
        assert db.startup_migrations().returncode == 0
        _seed(db)
        env = {**db.env(), "DATACONNECT_KAFKA_BOOTSTRAP": os.environ.get("DATACONNECT_KAFKA_BOOTSTRAP", "localhost:9092")}
        code = SCENARIO % {"pipe": PIPE, "topic": topic} + "\nprint('RESULT=' + json.dumps(asyncio.run(main())))\n"
        result = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, env=env, capture_output=True, text=True, timeout=420)
        assert result.returncode == 0, result.stderr[-3000:]
        out = json.loads(next(ln for ln in reversed(result.stdout.splitlines()) if ln.startswith("RESULT="))[len("RESULT="):])

    assert out["crashed"] is True                          # the process "died" on the Kafka commit
    assert out["after_crash_db"] == 10                     # ... after the database commit
    assert out["after_crash_kafka_committed"] == 0         # ... and Kafka has no committed offset, so it will redeliver everything
    assert out["replay_reached_kafka_commit"] is True      # the restarted worker re-read the ten records and committed their offsets
    assert out["after_replay_db"] == 10                    # THE REVIEW'S CHECK: the counter did not increase on the replay
    assert out["after_new_records_db"] == 15               # genuinely new records are still counted
    assert sum(out["marks"].values()) == 15                # the stored offsets cover all fifteen records across both partitions
