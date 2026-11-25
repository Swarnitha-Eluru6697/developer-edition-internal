"""
NEW-05, against a REAL Kafka broker and a REAL PostgreSQL: simulate a crash AFTER the quality worker has published its output but BEFORE it commits
its Kafka offset, restart it, and verify stable accounting.

Ten records (three invalid) go through the real QualityStreamWorker in ENFORCE mode. The first worker's Kafka commit is swallowed (it "dies" before
committing anything), so the restarted worker is handed all ten again. Expected: the statistics do not move (10 total, 3 failed), the outputs ARE
published again (at-least-once), and every repeat carries the same `inai.dedupe_key` as its original, so a deduplicating consumer sees 3 quarantined
and 7 prepared records exactly once.

A scratch database and throwaway topics are used and removed afterwards. Skipped unless BOTH DATACONNECT_REAL_DB_TESTS=1 and
DATACONNECT_REAL_KAFKA_TESTS=1 (the broker is DATACONNECT_KAFKA_BOOTSTRAP, default localhost:9092).
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
POLICY_ID = "00000000-0000-0000-0000-0000000000d1"

SCENARIO = '''
import asyncio, json, time
from sqlalchemy import text
from kafka import KafkaConsumer, KafkaProducer, TopicPartition
from kafka.admin import KafkaAdminClient
from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.services.quality_stream_worker import QualityStreamWorker

ENTITY = "@ENTITY@"
IN, QUARANTINE, PREPARED = "@IN@", "@QUARANTINE@", "@PREPARED@"
GROUP = "inai-quality-org_a-@PIPE@"
BOOTSTRAP = settings.kafka_bootstrap
STOCK = [50, 60, 5, 70, 80, 200, 90, 100, 110, 1]                                # offsets 2, 5, 9 are invalid
POLICY = {"id": "@POLICY@", "on_failure": "route_to_quarantine", "quarantine_sink": "kafka",
          "rules": [{"name": "stock_range", "type": "min_max_numeric", "field": "stock_quantity", "min": 20, "max": 150}]}
PIPELINE = {"id": "@PIPE@", "org_id": "org_a", "direction": "SINK", "zone": "raw", "domain": "d", "source_system": "s", "entity": ENTITY, "mode": "batch",
            "version": "v1", "topic_name": None, "schema_version_id": None, "connector_config": {"topics": IN},
            "quality_policy_id": "@POLICY@", "quality_mode": "ENFORCE"}

async def stats():
    async with AsyncSessionLocal() as s:
        r = (await s.execute(text("SELECT total_count, failed_count, jsonb_array_length(sample_failures) FROM dc_quality_stream_stats"))).all()
        rules = {k: int(n) for k, n in (await s.execute(text("SELECT key, sum(count) FROM dc_failure_counts WHERE kind = 'validation' GROUP BY 1"))).all()}
    return {"stats": list(r[0]) if r else [0, 0, 0], "rules": rules}

def committed_total(admin_unused=None):
    probe = KafkaConsumer(bootstrap_servers=BOOTSTRAP, group_id=GROUP, enable_auto_commit=False)
    try:
        return sum((probe.committed(TopicPartition(IN, p)) or 0) for p in probe.partitions_for_topic(IN) or [])
    finally:
        probe.close()

def read(topic):
    consumer = KafkaConsumer(topic, bootstrap_servers=BOOTSTRAP, auto_offset_reset="earliest", enable_auto_commit=False, consumer_timeout_ms=5000,
                             group_id="verify-" + ENTITY)
    out = [(dict((k, v.decode()) for k, v in (m.headers or [])), json.loads(m.value)) for m in consumer]
    consumer.close()
    return out

async def run_worker(*, swallow_commit, until):
    real_commit = KafkaConsumer.commit
    if swallow_commit:
        KafkaConsumer.commit = lambda self, *a, **kw: None             # the process "dies" before any offset reaches Kafka
    worker = QualityStreamWorker(org_id="org_a", pipeline=PIPELINE, policy=POLICY)
    worker.start()
    try:
        for _ in range(240):
            if (await stats())["stats"][0] >= until if swallow_commit else committed_total() >= 10:
                break
            await asyncio.sleep(0.25)
        await asyncio.sleep(1.5)
    finally:
        await worker.stop()
        KafkaConsumer.commit = real_commit

async def main():
    admin = KafkaAdminClient(bootstrap_servers=BOOTSTRAP)
    out = {}
    try:
        from app.services.kafka_admin import ensure_simple_topic
        await ensure_simple_topic(IN)
        producer = KafkaProducer(bootstrap_servers=BOOTSTRAP)
        for o, s in enumerate(STOCK):
            producer.send(IN, key=f"k{o}".encode(), value=json.dumps({"schema": {"type": "struct", "fields": []}, "payload": {"id": o, "stock_quantity": s}}).encode())
        producer.flush(); producer.close()

        await run_worker(swallow_commit=True, until=10)                  # records, publishes ... and "crashes" without committing
        out["after_crash"] = {**(await stats()), "kafka_committed": committed_total()}
        out["published_after_crash"] = {"quarantine": len(read(QUARANTINE)), "prepared": len(read(PREPARED))}

        await run_worker(swallow_commit=False, until=10)                 # the restart: all ten delivered again
        out["after_restart"] = {**(await stats()), "kafka_committed": committed_total()}
        quarantine, prepared = read(QUARANTINE), read(PREPARED)
        out["published"] = {"quarantine": len(quarantine), "prepared": len(prepared)}
        out["unique_keys"] = {"quarantine": len({h["inai.dedupe_key"] for h, _ in quarantine}), "prepared": len({h["inai.dedupe_key"] for h, _ in prepared})}
        out["quarantine_event_ids"] = sorted({v["event_id"].rsplit(":", 1)[1] for _, v in quarantine})
        out["every_message_has_a_key"] = all("inai.dedupe_key" in h for h, _ in quarantine + prepared)
    finally:
        for name in ("delete_topics",):
            try: getattr(admin, name)([IN, QUARANTINE, PREPARED])
            except Exception: pass
        for name in ("delete_groups", "delete_consumer_groups"):
            try:
                getattr(admin, name)([GROUP]); break
            except Exception: pass
        admin.close()
    return out
'''


def _seed(db, entity: str, topic: str) -> None:
    db.execute(f"""
        INSERT INTO dc_organizations (org_id, name, status) VALUES ('org_a', 'Org A', 'active');
        INSERT INTO dc_connector_templates (id, name, connector_type, category, connector_class, runtime_pool_type, config_schema, default_config,
            default_transforms, version, is_active, supported_transforms, supported_kafka_connect_versions, supported_source_versions,
            transform_compatibility, supported_modes, supports_dlq, supports_schema_discovery)
          VALUES ('tpl_a', 'T', 'sink', 'database', 'x.Y', 'connect-sink-pool', '{{}}', '{{}}', '[]', '1', true, '[]', '[]', '[]', '{{}}', '[]', true, false);
        INSERT INTO dc_connection_profiles (id, org_id, name, source_type, connection_config)
          VALUES ('00000000-0000-0000-0000-0000000000a1', 'org_a', 'conn', 'postgresql', '{{}}');
        INSERT INTO dc_quality_policies (id, org_id, name, rules, on_failure, quarantine_sink)
          VALUES ('{POLICY_ID}', 'org_a', 'p', '[]', 'route_to_quarantine', 'kafka');
        INSERT INTO dc_pipelines (id, org_id, name, template_id, connection_profile_id, zone, domain, source_system, entity, mode, version, direction,
            connector_name, connector_config, generated_config, template_snapshot, status, error_handling_policy, runtime_pool_id, quality_policy_id, quality_mode)
          VALUES ('{PIPE}', 'org_a', 'pipe', 'tpl_a', '00000000-0000-0000-0000-0000000000a1', 'raw', 'd', 's', '{entity}', 'batch', 'v1',
                  'SINK', 'c-{entity}', '{{"topics": "{topic}"}}', '{{}}', '{{}}', 'running', 'DLQ_AND_CONTINUE', 'connect-sink-pool', '{POLICY_ID}', 'ENFORCE');
    """)


def test_crash_after_publishing_but_before_the_kafka_commit_leaves_stable_accounting_and_identifiable_repeats() -> None:
    entity = f"qr{uuid.uuid4().hex[:8]}"
    topic_in = f"raw-d-s-{entity}-batch-v1"
    code = (SCENARIO
            .replace("@ENTITY@", entity).replace("@IN@", topic_in)
            .replace("@QUARANTINE@", f"quarantine-d-s-{entity}-validation-v1")
            .replace("@PREPARED@", f"prepared-d-s-{entity}-batch-v1")
            .replace("@PIPE@", PIPE).replace("@POLICY@", POLICY_ID)) + "\nprint('RESULT=' + json.dumps(asyncio.run(main())))\n"
    with scratch_database("dc_qreplay") as db:
        assert db.startup_migrations().returncode == 0
        _seed(db, entity, topic_in)
        env = {
            **db.env(), "DATACONNECT_KAFKA_BOOTSTRAP": os.environ.get("DATACONNECT_KAFKA_BOOTSTRAP", "localhost:9092"),
            # The worker creates its own topics. A single-node dev broker cannot hold the production default of 3 replicas.
            "DATACONNECT_TOPIC_DEFAULT_REPLICATION_FACTOR": os.environ.get("DATACONNECT_TOPIC_DEFAULT_REPLICATION_FACTOR", "1"),
            "DATACONNECT_TOPIC_DEFAULT_MIN_INSYNC_REPLICAS": os.environ.get("DATACONNECT_TOPIC_DEFAULT_MIN_INSYNC_REPLICAS", "1"),
        }
        result = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, env=env, capture_output=True, text=True, timeout=420)
        assert result.returncode == 0, result.stderr[-3000:]
        out = json.loads(next(ln for ln in reversed(result.stdout.splitlines()) if ln.startswith("RESULT="))[len("RESULT="):])

    # Before the restart: all ten recorded and published, and nothing committed to Kafka (the crash).
    assert out["after_crash"]["stats"] == [10, 3, 3]
    assert out["after_crash"]["kafka_committed"] == 0
    assert out["published_after_crash"] == {"quarantine": 3, "prepared": 7}
    # After the restart the same ten records were delivered again — and committed this time.
    assert out["after_restart"]["kafka_committed"] == 10
    assert out["after_restart"]["stats"] == [10, 3, 3]                      # THE REVIEW'S CHECK: stable accounting
    assert out["after_restart"]["rules"] == {"*": 3, "stock_range": 3}      # including the per-rule counts
    # The outputs were published again (at-least-once) ...
    assert out["published"] == {"quarantine": 6, "prepared": 14}
    # ... and every repeat carries the same dedupe key as its original, so a deduplicating consumer sees each record once.
    assert out["unique_keys"] == {"quarantine": 3, "prepared": 7}
    assert out["every_message_has_a_key"] is True
    assert out["quarantine_event_ids"] == ["2", "5", "9"]                   # the three invalid records' offsets
