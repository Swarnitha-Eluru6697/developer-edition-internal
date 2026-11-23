"""
NEW-04, against a REAL PostgreSQL: two pipelines share one connector dead-letter topic, and each pipeline gets exactly its own failures.

Before, the counter treated the FIRST pipeline of a shared topic as its owner and filed every record under it — so the second pipeline always
read zero, the owner read the sum, and deleting the owner (ON DELETE CASCADE) wiped the counts of every pipeline on the topic. Each record is
now counted under the pipeline whose connector wrote it, taken from the `__connect.errors.connector.name` header.

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
PIPE_C = "00000000-0000-0000-0000-0000000000c2"
SHARED_TOPIC = "quarantine-d-s-e-connector-v1"           # a, b and c all have domain d / source s / entity e / version v1

PRELUDE = '''
import asyncio, json, time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from sqlalchemy import text
from app.db.session import AsyncSessionLocal
from app.services import dlq_failure_counter as dlq
from app.services.dlq_attribution import DlqAttribution
from app.services.failure_counters import INGESTION, windowed_counts

PIPE_A, PIPE_B, PIPE_C = "%(a)s", "%(b)s", "%(c)s"
CONN_A, CONN_B, CONN_C = "d--s--e--batch--v1", "d--s--e--stream--v1", "d--s--e--replay--v1"
TOPIC = "%(topic)s"
OLDEST = datetime.now(UTC) - timedelta(days=1)

def rec(partition, offset, connector, category="serialization"):
    ser = category == "serialization"
    headers = [
        ("__connect.errors.stage", b"VALUE_CONVERTER" if ser else b"TASK_PUT"),
        ("__connect.errors.exception.class.name", b"org.apache.kafka.connect.errors.DataException" if ser else b"java.sql.SQLException"),
        ("__connect.errors.exception.message", b"Converting byte[] to Kafka Connect data failed due to serialization error" if ser else b"connection reset"),
    ]
    if connector:
        headers.append(("__connect.errors.connector.name", connector.encode()))
    return SimpleNamespace(topic=TOPIC, partition=partition, offset=offset, timestamp=int(time.time() * 1000), headers=headers)

async def rows(sql):
    async with AsyncSessionLocal() as s:
        return [tuple(r) for r in (await s.execute(text(sql))).all()]

async def per_pipeline():
    return {p: int(n) for p, n in await rows("SELECT pipeline_id::text, sum(count) FROM dc_failure_counts WHERE kind = 'ingestion' GROUP BY 1")}

ATTR_AB = DlqAttribution([{"id": PIPE_A, "connector_name": CONN_A}, {"id": PIPE_B, "connector_name": CONN_B}])
'''.replace("%(a)s", PIPE_A).replace("%(b)s", PIPE_B).replace("%(c)s", PIPE_C).replace("%(topic)s", SHARED_TOPIC)


def _pipeline_sql(pid: str, mode: str, name: str) -> str:
    return f"""
        INSERT INTO dc_pipelines (id, org_id, name, template_id, connection_profile_id, zone, domain, source_system, entity, mode, version, direction,
            connector_name, connector_config, generated_config, template_snapshot, status, error_handling_policy, runtime_pool_id)
          VALUES ('{pid}', 'org_a', '{name}', 'tpl_a', '00000000-0000-0000-0000-0000000000a1', 'raw', 'd', 's', 'e', '{mode}', 'v1',
                  'SINK', 'd--s--e--{mode}--v1', '{{"topics": "raw-d-s-in-batch-v1"}}', '{{}}', '{{}}', 'running', 'DLQ_AND_CONTINUE', 'connect-sink-pool');
    """


@pytest.fixture
def db():
    with scratch_database("dc_dlqshared") as scratch:
        assert scratch.startup_migrations().returncode == 0
        # These tests are about how the counter behaves when two sinks share one dead-letter topic. Since NEW-08 the database refuses that state, so
        # the scratch database models one from BEFORE that rule (a shared topic that predates the index) by dropping the index. The counter's
        # per-record attribution is kept as defence in depth for exactly such rows.
        scratch.execute("DROP INDEX uq_dc_pipelines_dead_letter_topic")
        scratch.execute("""
            INSERT INTO dc_organizations (org_id, name, status) VALUES ('org_a', 'Org A', 'active');
            INSERT INTO dc_connector_templates (id, name, connector_type, category, connector_class, runtime_pool_type, config_schema, default_config,
                default_transforms, version, is_active, supported_transforms, supported_kafka_connect_versions, supported_source_versions,
                transform_compatibility, supported_modes, supports_dlq, supports_schema_discovery)
              VALUES ('tpl_a', 'T', 'sink', 'database', 'x.Y', 'connect-sink-pool', '{}', '{}', '[]', '1', true, '[]', '[]', '[]', '{}', '[]', true, false);
            INSERT INTO dc_connection_profiles (id, org_id, name, source_type, connection_config)
              VALUES ('00000000-0000-0000-0000-0000000000a1', 'org_a', 'conn', 'postgresql', '{}');
        """)
        scratch.execute(_pipeline_sql(PIPE_A, "batch", "pipe-a"))      # the FIRST pipeline on the topic — the old "owner"
        scratch.execute(_pipeline_sql(PIPE_B, "stream", "pipe-b"))
        yield scratch


def _run(scratch, body: str, timeout: int = 120):
    code = PRELUDE + "\n" + body + "\nprint('RESULT=' + json.dumps(asyncio.run(main()), default=str))\n"
    result = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, env=scratch.env(), capture_output=True, text=True, timeout=timeout)
    assert result.returncode == 0, result.stderr[-2500:]
    line = next(ln for ln in reversed(result.stdout.splitlines()) if ln.startswith("RESULT="))
    return json.loads(line[len("RESULT="):])


# ── The review's check ─────────────────────────────────────────────────────────────────────────────────────────────────

def test_failures_injected_for_two_pipelines_sharing_a_dlq_are_counted_separately(db) -> None:
    result = _run(db, '''
async def main():
    batch = [rec(0, o, CONN_A) for o in range(3)] + [rec(0, 3 + o, CONN_B) for o in range(2)]
    outcome = await dlq.record_batch_once(org_id="org_a", topic=TOPIC, records=batch, oldest=OLDEST, attribution=ATTR_AB)
    return {"outcome": [outcome.counted, outcome.replayed, outcome.unattributed], "per_pipeline": await per_pipeline()}
''')
    assert result["outcome"] == [5, 0, 0]
    assert result["per_pipeline"] == {PIPE_A: 3, PIPE_B: 2}            # the second pipeline is no longer zero, the first no longer the sum


def test_the_causes_are_counted_per_pipeline(db) -> None:
    result = _run(db, '''
async def main():
    batch = [rec(0, 0, CONN_A, "serialization"), rec(0, 1, CONN_A, "serialization"), rec(0, 2, CONN_B, "connector")]
    await dlq.record_batch_once(org_id="org_a", topic=TOPIC, records=batch, oldest=OLDEST, attribution=ATTR_AB)
    return await rows("SELECT pipeline_id::text, key, sum(count) FROM dc_failure_counts WHERE kind = 'ingestion' GROUP BY 1, 2 ORDER BY 1, 2")
''')
    assert [(p, k, int(n)) for p, k, n in result] == [(PIPE_A, "serialization", 2), (PIPE_B, "connector", 1)]


def test_records_nobody_can_be_blamed_for_are_counted_for_no_pipeline(db) -> None:
    result = _run(db, '''
async def main():
    batch = [rec(0, 0, CONN_A), rec(0, 1, None), rec(0, 2, "gone--x--y--batch--v1"), rec(0, 3, CONN_B)]
    outcome = await dlq.record_batch_once(org_id="org_a", topic=TOPIC, records=batch, oldest=OLDEST, attribution=ATTR_AB)
    return {"outcome": [outcome.counted, outcome.replayed, outcome.unattributed], "per_pipeline": await per_pipeline(),
            "marks": {p: n for p, n in await rows("SELECT kafka_partition, next_offset FROM dc_failure_count_offsets")}}
''')
    assert result["outcome"] == [2, 0, 2]
    assert result["per_pipeline"] == {PIPE_A: 1, PIPE_B: 1}
    assert result["marks"] == {"0": 4}                                  # all four were READ, so a replay does not re-evaluate them


def test_a_replay_after_a_crash_does_not_change_either_pipelines_count(db) -> None:
    """NEW-03 still holds with several pipelines on the topic."""
    result = _run(db, '''
async def main():
    batch = [rec(0, o, CONN_A) for o in range(3)] + [rec(0, 3 + o, CONN_B) for o in range(2)]
    await dlq.record_batch_once(org_id="org_a", topic=TOPIC, records=batch, oldest=OLDEST, attribution=ATTR_AB)
    replay = await dlq.record_batch_once(org_id="org_a", topic=TOPIC, records=batch, oldest=OLDEST, attribution=ATTR_AB)
    return {"replay": [replay.counted, replay.replayed], "per_pipeline": await per_pipeline()}
''')
    assert result == {"replay": [0, 5], "per_pipeline": {PIPE_A: 3, PIPE_B: 2}}


# ── Reading per pipeline, through the real SQL ─────────────────────────────────────────────────────────────────────────

def test_the_windowed_counts_can_be_read_for_one_pipeline(db) -> None:
    result = _run(db, '''
async def main():
    batch = [rec(0, o, CONN_A) for o in range(4)] + [rec(0, 4 + o, CONN_B, "connector") for o in range(3)]
    await dlq.record_batch_once(org_id="org_a", topic=TOPIC, records=batch, oldest=OLDEST, attribution=ATTR_AB)
    return {
        "org": await windowed_counts("org_a", INGESTION, 3600),
        "a": await windowed_counts("org_a", INGESTION, 3600, PIPE_A),
        "b": await windowed_counts("org_a", INGESTION, 3600, PIPE_B),
        "other_org": await windowed_counts("org_other", INGESTION, 3600, PIPE_A),
    }
''')
    assert result == {"org": {"serialization": 4, "connector": 3}, "a": {"serialization": 4}, "b": {"connector": 3}, "other_org": {}}


# ── Deleting a pipeline ────────────────────────────────────────────────────────────────────────────────────────────────

def test_deleting_the_first_pipeline_no_longer_takes_the_other_pipelines_counts_with_it(db) -> None:
    result = _run(db, '''
async def main():
    batch = [rec(0, o, CONN_A) for o in range(3)] + [rec(0, 3 + o, CONN_B) for o in range(2)]
    await dlq.record_batch_once(org_id="org_a", topic=TOPIC, records=batch, oldest=OLDEST, attribution=ATTR_AB)
    async with AsyncSessionLocal() as s:
        await s.execute(text(f"DELETE FROM dc_pipelines WHERE id = '{PIPE_A}'"))       # the old "owner" goes
        await s.commit()
    return {"per_pipeline": await per_pipeline(), "org": await windowed_counts("org_a", INGESTION, 3600)}
''')
    assert result["per_pipeline"] == {PIPE_B: 2}          # B keeps its own two; before, ALL FIVE sat under A and vanished with it
    assert result["org"] == {"serialization": 2}


def test_after_the_first_pipeline_is_deleted_the_rest_of_the_topic_is_not_counted_again(db) -> None:
    result = _run(db, '''
async def main():
    batch = [rec(0, o, CONN_A) for o in range(3)] + [rec(0, 3 + o, CONN_B) for o in range(2)]
    await dlq.record_batch_once(org_id="org_a", topic=TOPIC, records=batch, oldest=OLDEST, attribution=ATTR_AB)
    async with AsyncSessionLocal() as s:
        await s.execute(text(f"DELETE FROM dc_pipelines WHERE id = '{PIPE_A}'"))
        await s.commit()
    only_b = DlqAttribution([{"id": PIPE_B, "connector_name": CONN_B}])
    # the topic's new first pipeline gets a new consumer group, which starts from the beginning: the same five records arrive again
    replay = await dlq.record_batch_once(org_id="org_a", topic=TOPIC, records=batch, oldest=OLDEST, attribution=only_b)
    return {"replay": [replay.counted, replay.replayed], "per_pipeline": await per_pipeline()}
''')
    assert result == {"replay": [0, 5], "per_pipeline": {PIPE_B: 2}}      # the offsets belong to the topic, so B's two are not doubled


# ── The manager follows the topic ──────────────────────────────────────────────────────────────────────────────────────

def test_the_manager_runs_one_worker_per_shared_topic_and_follows_its_pipelines(db) -> None:
    db.execute(_pipeline_sql(PIPE_C, "replay", "pipe-c").replace("'running'", "'draft'"))      # a draft is not deployed: not counted yet
    result = _run(db, '''
async def main():
    started = []
    dlq.DlqCounterWorker.start = lambda self: started.append(self)             # no Kafka: just see who the manager would run
    manager = dlq.DlqCounterManager()
    await manager.reconcile_once()
    first = list(manager._workers.values())
    snapshot = lambda w: {"owner": w.pipeline_id, "connectors": sorted(w.attribution.by_connector)}
    out = {"workers": len(first), "first": snapshot(first[0]), "topic": first[0].topic}

    async with AsyncSessionLocal() as s:                                         # the third pipeline is deployed
        await s.execute(text(f"UPDATE dc_pipelines SET status = 'running' WHERE id = '{PIPE_C}'"))
        await s.commit()
    await manager.reconcile_once()
    same = list(manager._workers.values())
    out["after_third"] = {"workers": len(same), "same_worker": same[0] is first[0], **snapshot(same[0])}

    async with AsyncSessionLocal() as s:                                         # the first (owner) pipeline is deleted
        await s.execute(text(f"DELETE FROM dc_pipelines WHERE id = '{PIPE_A}'"))
        await s.commit()
    await manager.reconcile_once()
    after = list(manager._workers.values())
    out["after_delete"] = {"workers": len(after), "replaced": after[0] is not first[0], **snapshot(after[0])}
    out["started"] = len(started)
    return out
''')
    assert result["workers"] == 1
    assert result["topic"] == SHARED_TOPIC
    assert result["first"] == {"owner": PIPE_A, "connectors": ["d--s--e--batch--v1", "d--s--e--stream--v1"]}
    assert result["after_third"] == {"workers": 1, "same_worker": True, "owner": PIPE_A,           # the new pipeline joins WITHOUT a restart
                                     "connectors": ["d--s--e--batch--v1", "d--s--e--replay--v1", "d--s--e--stream--v1"]}
    assert result["after_delete"] == {"workers": 1, "replaced": True, "owner": PIPE_B,             # only the group-naming owner changes
                                      "connectors": ["d--s--e--replay--v1", "d--s--e--stream--v1"]}


def test_a_worker_reports_the_records_it_could_not_attribute_in_a_metric(db) -> None:
    """Records counted for no pipeline must not vanish silently: the worker logs them and counts them in
    dataconnect_dlq_unattributed_records_total."""
    result = _run(db, '''
from prometheus_client import REGISTRY
from app.core.metrics import SERVICE_NAME

def metric():
    return REGISTRY.get_sample_value("dataconnect_dlq_unattributed_records_total", {"service": SERVICE_NAME}) or 0.0

class FakeConsumer:
    def __init__(self): self.served, self.commits = False, 0
    def poll(self, timeout_ms, max_records):
        time.sleep(0.01)
        if self.served:
            return {}
        self.served = True
        return {("tp", 0): [rec(0, 0, CONN_A), rec(0, 1, None), rec(0, 2, "gone--x--y--batch--v1"), rec(0, 3, CONN_B)]}
    def commit(self): self.commits += 1
    def close(self): pass
    def partitions_for_topic(self, topic): return {0}
    def end_offsets(self, partitions): return {tp: 4 for tp in partitions}

async def main():
    before = metric()
    consumer = FakeConsumer()
    dlq._open_consumer = lambda topic, group: consumer
    worker = dlq.DlqCounterWorker(org_id="org_a", pipeline_id=PIPE_A, topic=TOPIC, attribution=ATTR_AB)
    task = asyncio.create_task(worker._run())
    for _ in range(300):
        if consumer.commits:
            break
        await asyncio.sleep(0.02)
    worker._stopping = True
    await asyncio.wait_for(task, 10)
    return {"reported": metric() - before, "per_pipeline": await per_pipeline()}
''')
    assert result["reported"] == 2.0
    assert result["per_pipeline"] == {PIPE_A: 1, PIPE_B: 1}
