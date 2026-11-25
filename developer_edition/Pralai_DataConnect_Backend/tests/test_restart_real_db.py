"""
Hardening, against a REAL PostgreSQL and REAL processes: restart during active operations.

A worker process is SIGKILLed (no cleanup, no `finally`, no goodbye) while it is in the middle of work, and a replacement process must finish it:

  * an OPERATION claimed by the dead worker is reclaimed once its lease lapses, and runs to completion — it is neither lost nor left "running"
    forever, and it is not run a third time;
  * a QUALITY JOB killed between chunks RESUMES at its checkpoint: chunks already finished are not redone (only the chunk that was in flight is);
  * several workers claiming at once never take the same row (the real `FOR UPDATE SKIP LOCKED` SQL);
  * a row whose work keeps killing its worker is eventually given up on (`stale`) instead of being reclaimed for ever.

The delivery guarantee is AT-LEAST-ONCE: the in-flight unit of work can run twice. That is the documented design (work must be idempotent or
deduplicated — quality quarantine writes carry a stable dedupe key); these tests pin its exact shape so it cannot silently become at-most-once
(lost) or many-times (stuck).

Scratch database (tests/scratch_db.py); skipped unless DATACONNECT_REAL_DB_TESTS=1.
"""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from scratch_db import BACKEND, scratch_database

pytestmark = pytest.mark.skipif(not os.environ.get("DATACONNECT_REAL_DB_TESTS"), reason="needs a real PostgreSQL (DATACONNECT_REAL_DB_TESTS=1)")

ORG = "org_r"

OPERATION_WORKER = r"""
import asyncio, os, sys
from pydantic import BaseModel
from app.services import operation_runner

role = sys.argv[1]
marker = os.environ["MARKER"]

class Result(BaseModel):
    ran_by: str

def factory(org_id, resource_id):
    async def work():
        with open(marker, "a") as f:
            f.write(f"{role}:start\n")
        if role == "victim":
            await asyncio.sleep(300)             # will be killed in here
        with open(marker, "a") as f:
            f.write(f"{role}:done\n")
        return {"ran_by": role}
    return work

operation_runner.register_work("restart_test_op", factory, Result)

async def main():
    if role == "victim":
        await operation_runner.create_and_run(org_id=os.environ["ORG"], operation_type="restart_test_op", resource_type="thing", resource_id="r1")
    await operation_runner.start_worker()
    await asyncio.sleep(300)

asyncio.run(main())
"""

QUALITY_WORKER = r"""
import asyncio, os, sys
from app.services import kafka_runtime, quality_job_runner
from app.services.kafka_runtime import InMemoryKafkaProducer
from app.services.quality_service import quality_service

role = sys.argv[1]
marker = os.environ["MARKER"]
producer = InMemoryKafkaProducer()

async def get_producer():
    return producer
kafka_runtime.get_kafka_producer = get_producer

async def process_job_chunk(**kw):
    index = int(kw["dedupe_prefix"].rsplit(":", 1)[1])
    with open(marker, "a") as f:
        f.write(f"{role}:chunk{index}\n")
    if role == "victim" and index == 1:
        await asyncio.sleep(300)                 # killed in the middle of chunk 1, AFTER chunk 0 was checkpointed
    return {"failed": 1, "total": len(kw["records"]), "samples": []}
quality_service.process_job_chunk = process_job_chunk

async def main():
    if role == "victim":
        await quality_service.submit_execution_job(
            org_id=os.environ["ORG"], pipeline={"id": os.environ["PIPELINE"], "topic_name": "t", "direction": "SOURCE"},
            policy={"id": os.environ["POLICY"]}, records=[{"n": i} for i in range(4)])
    await quality_job_runner.start_worker()
    await asyncio.sleep(300)

asyncio.run(main())
"""


def _spawn(db, script: str, role: str, marker: Path, **extra_env) -> subprocess.Popen:
    env = {**db.env(), "MARKER": str(marker), "ORG": ORG,
           "DATACONNECT_OPERATION_LEASE_SECONDS": "2", "DATACONNECT_OPERATION_POLL_INTERVAL_SECONDS": "0.2",
           "DATACONNECT_QUALITY_JOB_LEASE_SECONDS": "2", "DATACONNECT_QUALITY_JOB_POLL_INTERVAL_SECONDS": "0.2",
           "DATACONNECT_QUALITY_JOB_CHUNK_RECORDS": "1", **extra_env}
    return subprocess.Popen([sys.executable, "-c", script, role], cwd=BACKEND, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)


def _lines(marker: Path) -> list[str]:
    return marker.read_text().splitlines() if marker.exists() else []


def _wait(condition, timeout: float, what: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


def _kill(process: subprocess.Popen) -> None:
    process.send_signal(signal.SIGKILL)         # no handler can run: this is a crash, not a shutdown
    process.wait(timeout=10)


@pytest.fixture
def db():
    with scratch_database("dc_restart") as database:
        assert database.startup_migrations().returncode == 0
        database.execute(f"INSERT INTO dc_organizations (org_id, name, status) VALUES ('{ORG}', 'r', 'active')")
        yield database


def _stop(*processes: subprocess.Popen) -> None:
    for process in processes:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)


# ── operations ─────────────────────────────────────────────────────────────────────────────────────────────────────

def test_an_operation_in_flight_when_its_worker_is_killed_is_reclaimed_and_completes_exactly_once_more(db, tmp_path) -> None:
    marker = tmp_path / "ops.log"
    victim = _spawn(db, OPERATION_WORKER, "victim", marker)
    replacement = None
    try:
        _wait(lambda: "victim:start" in _lines(marker), 60, "the victim to start the work")
        row = db.query("SELECT status, attempt_count FROM dc_operations")[0]
        assert (row[0], row[1]) == ("running", 1)

        _kill(victim)                                                          # SIGKILL mid-work
        assert db.query("SELECT status FROM dc_operations")[0][0] == "running"  # nothing marked it failed: it is simply abandoned

        replacement = _spawn(db, OPERATION_WORKER, "replacement", marker)
        _wait(lambda: db.query("SELECT status FROM dc_operations")[0][0] == "succeeded", 60, "the replacement to finish it")
    finally:
        _stop(victim, *(p for p in [replacement] if p))

    row = db.query("SELECT status, attempt_count, result_status, result::text, error_code FROM dc_operations")[0]
    assert row[0] == "succeeded"
    assert row[1] == 2                                                         # claimed by the victim, then reclaimed — not a third time
    assert row[2] == "valid"
    assert json.loads(row[3]) == {"ran_by": "replacement"}
    assert row[4] is None
    assert _lines(marker) == ["victim:start", "replacement:start", "replacement:done"]   # the work completed once; started twice (at-least-once)
    assert db.scalar("SELECT count(*) FROM dc_operations") == 1                # not duplicated, not lost


def test_an_operation_is_not_reclaimed_while_its_worker_is_alive_and_heartbeating(db, tmp_path) -> None:
    """The lease is 2s but the work runs far longer: only the heartbeat keeps a second worker from stealing it."""
    marker = tmp_path / "ops.log"
    victim = _spawn(db, OPERATION_WORKER, "victim", marker)
    bystander = None
    try:
        _wait(lambda: "victim:start" in _lines(marker), 60, "the victim to start the work")
        bystander = _spawn(db, OPERATION_WORKER, "bystander", marker)        # only now: it must find the operation already leased
        time.sleep(7)                                                          # more than three lease lengths

        assert _lines(marker) == ["victim:start"]                              # the bystander never took it
        assert db.query("SELECT status, attempt_count FROM dc_operations")[0][1] == 1
    finally:
        _stop(victim, *(p for p in [bystander] if p))


def test_a_row_whose_work_keeps_killing_its_worker_is_eventually_marked_stale_not_reclaimed_for_ever(db, tmp_path) -> None:
    marker = tmp_path / "ops.log"
    db.execute(f"""INSERT INTO dc_operations (id, org_id, operation_type, resource_type, resource_id, status, attempt_count, lease_expires_at)
                   VALUES ('00000000-0000-0000-0000-000000000001', '{ORG}', 'restart_test_op', 'thing', 'r1', 'running', 99, now() - interval '1 minute')""")
    worker = _spawn(db, OPERATION_WORKER, "replacement", marker)
    try:
        _wait(lambda: db.query("SELECT status FROM dc_operations")[0][0] == "stale", 60, "the poisoned row to be given up on")
    finally:
        _stop(worker)

    row = db.query("SELECT status, error_code, retryable FROM dc_operations")[0]
    assert (row[0], row[1], row[2]) == ("stale", "ATTEMPTS_EXHAUSTED", False)
    assert _lines(marker) == []                                               # and its work was NOT run yet again


CLAIM_RACE = r"""
import asyncio, json, os
from app.services import operation_runner
async def main():
    claimed = []
    async def claimer():
        while (op := await operation_runner._claim_next()) is not None:
            claimed.append(op["id"])
    await asyncio.gather(*(claimer() for _ in range(8)))
    print(json.dumps(claimed))
asyncio.run(main())
"""


def test_eight_workers_claiming_at_once_take_every_queued_operation_exactly_once(db) -> None:
    values = ",".join(f"('00000000-0000-0000-0000-{i:012d}', '{ORG}', 'restart_test_op', 'thing', 'r{i}', 'queued')" for i in range(1, 41))
    db.execute(f"INSERT INTO dc_operations (id, org_id, operation_type, resource_type, resource_id, status) VALUES {values}")

    result = subprocess.run([sys.executable, "-c", CLAIM_RACE], cwd=BACKEND, env=db.env(), capture_output=True, text=True, timeout=120)

    assert result.returncode == 0, result.stderr[-1500:]
    claimed = json.loads(result.stdout.strip().splitlines()[-1])
    assert len(claimed) == 40
    assert len(set(claimed)) == 40                                            # no operation handed to two claimers
    assert db.scalar("SELECT count(*) FROM dc_operations WHERE status = 'running' AND attempt_count = 1") == 40


# ── quality jobs ───────────────────────────────────────────────────────────────────────────────────────────────────

def _seed_job_parents(db) -> dict:
    db.execute(f"""
        INSERT INTO dc_connector_templates (id, name, connector_type, category, connector_class, runtime_pool_type, config_schema, default_config,
            default_transforms, version, is_active, supported_transforms, supported_kafka_connect_versions, supported_source_versions,
            transform_compatibility, supported_modes, supports_dlq, supports_schema_discovery)
          VALUES ('tpl_r', 'T', 'source', 'database', 'x.Y', 'connect-jdbc-pool', '{{}}', '{{}}', '[]', '1', true, '[]', '[]', '[]', '{{}}', '[]', false, false);
        INSERT INTO dc_connection_profiles (id, org_id, name, source_type, connection_config)
          VALUES ('00000000-0000-0000-0000-0000000000c1', '{ORG}', 'c', 'postgresql', '{{}}');
        INSERT INTO dc_pipelines (id, org_id, name, template_id, connection_profile_id, zone, domain, source_system, entity, mode, version, direction,
            connector_name, connector_config, generated_config, template_snapshot, status, error_handling_policy, runtime_pool_id, topic_name)
          VALUES ('00000000-0000-0000-0000-0000000000a1', '{ORG}', 'p', 'tpl_r', '00000000-0000-0000-0000-0000000000c1', 'raw', 'd', 's', 'e', 'batch',
                  'v1', 'SOURCE', 'c', '{{"tables": ["t"]}}', '{{}}', '{{}}', 'running', 'FAIL_FAST', 'connect-jdbc-pool', 'raw-d-s-e-batch-v1');
        INSERT INTO dc_quality_policies (id, org_id, name, rules, on_failure, quarantine_sink)
          VALUES ('00000000-0000-0000-0000-0000000000d1', '{ORG}', 'pol', '[]', 'route_to_quarantine', 'kafka');
    """)
    return {"PIPELINE": "00000000-0000-0000-0000-0000000000a1", "POLICY": "00000000-0000-0000-0000-0000000000d1"}


def test_a_quality_job_killed_between_chunks_resumes_at_its_checkpoint_and_redoes_only_the_chunk_in_flight(db, tmp_path) -> None:
    marker = tmp_path / "job.log"
    env = _seed_job_parents(db)
    victim = _spawn(db, QUALITY_WORKER, "victim", marker, **env)
    replacement = None
    try:
        _wait(lambda: "victim:chunk1" in _lines(marker), 90, "the victim to reach chunk 1")
        # chunk 0 is finished and its checkpoint committed; chunk 1 is in flight
        assert db.scalar("SELECT (progress ->> 'chunks_completed')::int FROM dc_quality_execution_jobs") == 1

        _kill(victim)
        assert db.scalar("SELECT status FROM dc_quality_execution_jobs") == "running"

        replacement = _spawn(db, QUALITY_WORKER, "replacement", marker, **env)
        _wait(lambda: db.scalar("SELECT status FROM dc_quality_execution_jobs") == "succeeded", 90, "the replacement to finish the job")
    finally:
        _stop(victim, *(p for p in [replacement] if p))

    # chunk 0 ran ONCE (by the victim, never redone); chunk 1 was in flight when it died, so the replacement redid it, then 2 and 3
    assert _lines(marker) == ["victim:chunk0", "victim:chunk1", "replacement:chunk1", "replacement:chunk2", "replacement:chunk3"]
    job = db.query("SELECT status, attempt_count, error_code, result_id FROM dc_quality_execution_jobs")[0]
    assert (job[0], job[1], job[2]) == ("succeeded", 2, None)
    result = db.query(f"SELECT total_count, failed_count FROM dc_quality_results WHERE id = '{job[3]}'")[0]
    assert (result[0], result[1]) == (4, 4)                                  # totals carried across the crash: 4 records, none double counted
    assert db.scalar("SELECT count(*) FROM dc_quality_execution_job_records") == 0   # input cleaned up on success
    assert db.scalar("SELECT count(*) FROM dc_quality_results") == 1          # exactly one result for the job
