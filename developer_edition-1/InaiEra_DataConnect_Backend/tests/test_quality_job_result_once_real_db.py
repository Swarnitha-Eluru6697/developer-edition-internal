"""
NEW-05 (batch jobs), against a REAL PostgreSQL: a quality execution job has exactly one QualityResult, even when the worker dies after committing it
and before marking the job finished.

Before: the result was committed, then the job was marked `succeeded` in a separate step. A crash in between left the job `running` with every chunk
already done; the worker that reclaimed it finalized it AGAIN and wrote a second result for the same job (and emitted it to UCS twice), so the
"quality results" list showed duplicates. `dc_quality_results.job_id` is now UNIQUE and the write is idempotent.

Each test runs the app code in a subprocess against a scratch database (tests/scratch_db.py). Skipped unless DATACONNECT_REAL_DB_TESTS=1.
"""

import json
import os
import subprocess
import sys

import pytest
from scratch_db import BACKEND, scratch_database

pytestmark = pytest.mark.skipif(not os.environ.get("DATACONNECT_REAL_DB_TESTS"), reason="needs a real PostgreSQL (DATACONNECT_REAL_DB_TESTS=1)")

PIPE = "00000000-0000-0000-0000-0000000000a2"
POLICY_ID = "00000000-0000-0000-0000-0000000000d1"

PRELUDE = '''
import asyncio, json
from datetime import UTC, datetime, timedelta
from sqlalchemy import text
from app.db.session import AsyncSessionLocal
from app.repositories.dataconnect import QualityExecutionJobRepository, QualityResultRepository
from app.services import quality_job_runner as qjr
from app.services import quality_service as qs_module
from app.services.kafka_runtime import InMemoryKafkaProducer
from app.services.pipeline_service import pipeline_service
from app.services.quality_service import quality_service

PIPE, POLICY_ID = "@PIPE@", "@POLICY@"
RECORDS = [{"id": i, "stock_quantity": 50 if i != 2 else 1} for i in range(4)]          # one of four fails

kafka = InMemoryKafkaProducer()
async def _producer(): return kafka
import app.services.kafka_runtime as kafka_runtime
kafka_runtime.get_kafka_producer = _producer      # the runner imports it INSIDE _execute, so patch the source: never touch a real broker

emitted = []
async def _emit(org_id, policy, result): emitted.append(result["id"])                  # what would be sent to UCS
qs_module.emit_quality_result = _emit

async def rows(sql):
    async with AsyncSessionLocal() as s:
        return [tuple(r) for r in (await s.execute(text(sql))).all()]

async def submit():
    pipeline = await pipeline_service.get_pipeline("org_a", PIPE)
    policy = await quality_service.get_policy("org_a", POLICY_ID)
    job = await quality_service.submit_execution_job(org_id="org_a", pipeline=pipeline, policy=policy, records=RECORDS)
    return job["id"]

async def results_of(job_id):
    return await rows(f"SELECT id::text, failed_count, total_count, status FROM dc_quality_results WHERE job_id = '{job_id}'")

async def job_row(job_id):
    r = await rows(f"SELECT status, result_id::text FROM dc_quality_execution_jobs WHERE id = '{job_id}'")
    return list(r[0])
'''.replace("@PIPE@", PIPE).replace("@POLICY@", POLICY_ID)


@pytest.fixture
def db():
    with scratch_database("dc_jobresult") as scratch:
        assert scratch.startup_migrations().returncode == 0
        scratch.execute(f"""
            INSERT INTO dc_organizations (org_id, name, status) VALUES ('org_a', 'Org A', 'active');
            INSERT INTO dc_connector_templates (id, name, connector_type, category, connector_class, runtime_pool_type, config_schema, default_config,
                default_transforms, version, is_active, supported_transforms, supported_kafka_connect_versions, supported_source_versions,
                transform_compatibility, supported_modes, supports_dlq, supports_schema_discovery)
              VALUES ('tpl_a', 'T', 'source', 'database', 'x.Y', 'connect-jdbc-pool', '{{}}', '{{}}', '[]', '1', true, '[]', '[]', '[]', '{{}}', '[]', false, false);
            INSERT INTO dc_connection_profiles (id, org_id, name, source_type, connection_config)
              VALUES ('00000000-0000-0000-0000-0000000000a1', 'org_a', 'conn', 'postgresql', '{{}}');
            INSERT INTO dc_quality_policies (id, org_id, name, rules, on_failure, quarantine_sink)
              VALUES ('{POLICY_ID}', 'org_a', 'p', '[{{"name": "stock_range", "type": "min_max_numeric", "field": "stock_quantity", "min": 20, "max": 150}}]',
                      'route_to_quarantine', 'kafka');
            INSERT INTO dc_pipelines (id, org_id, name, template_id, connection_profile_id, zone, domain, source_system, entity, mode, version, direction,
                connector_name, connector_config, generated_config, template_snapshot, status, error_handling_policy, runtime_pool_id, topic_name)
              VALUES ('{PIPE}', 'org_a', 'pipe', 'tpl_a', '00000000-0000-0000-0000-0000000000a1', 'raw', 'd', 's', 'e', 'batch', 'v1', 'SOURCE',
                      'd--s--e--batch--v1', '{{"tables": ["t"]}}', '{{}}', '{{}}', 'running', 'SKIP_AND_LOG', 'connect-jdbc-pool', 'raw-d-s-e-batch-v1');
        """)
        yield scratch


def _run(scratch, body: str, timeout: int = 120):
    code = PRELUDE + "\n" + body + "\nprint('RESULT=' + json.dumps(asyncio.run(main()), default=str))\n"
    result = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, env=scratch.env(), capture_output=True, text=True, timeout=timeout)
    assert result.returncode == 0, result.stderr[-2500:]
    line = next(ln for ln in reversed(result.stdout.splitlines()) if ln.startswith("RESULT="))
    return json.loads(line[len("RESULT="):])


CRASH_AND_RECLAIM = '''
async def main():
    job_id = await submit()
    claimed = await qjr.claim_next_job()

    real_update = QualityExecutionJobRepository.update
    armed = {"on": True}
    async def dies_when_marking_the_job_finished(self, row, values):
        if armed["on"] and values.get("status") == "succeeded":
            armed["on"] = False
            raise RuntimeError("the process died after the result was committed")
        return await real_update(self, row, values)
    QualityExecutionJobRepository.update = dies_when_marking_the_job_finished
    try:
        await qjr._execute(claimed)
        died = False
    except RuntimeError:
        died = True
    after_crash = {"results": await results_of(job_id), "job": await job_row(job_id), "emitted": len(emitted)}

    async with AsyncSessionLocal() as s:                         # the lease lapses; the next worker's poll reclaims the job
        await s.execute(text(f"UPDATE dc_quality_execution_jobs SET lease_expires_at = now() - interval '1 minute' WHERE id = '{job_id}'"))
        await s.commit()
    reclaimed = await qjr.claim_next_job()
    await qjr._execute(reclaimed)
    return {"died": died, "reclaimed_the_same_job": reclaimed["id"] == job_id, "after_crash": after_crash,
            "after_reclaim": {"results": await results_of(job_id), "job": await job_row(job_id), "emitted": len(emitted)}}
'''


def test_a_crash_after_the_result_is_committed_but_before_the_job_is_finished_leaves_one_result(db) -> None:
    result = _run(db, CRASH_AND_RECLAIM)

    assert result["died"] is True
    assert result["reclaimed_the_same_job"] is True
    # The crash left the job running with its result already stored ...
    assert len(result["after_crash"]["results"]) == 1
    assert result["after_crash"]["job"][0] == "running"
    # ... and after the reclaim there is STILL exactly one result, the job points at it, and UCS was told once.
    (stored,) = result["after_reclaim"]["results"]
    assert stored[1:] == [1, 4, "failed"]                                    # 1 failed of 4
    assert result["after_reclaim"]["job"] == ["succeeded", stored[0]]       # the job's result_id is THE result
    assert result["after_reclaim"]["results"] == result["after_crash"]["results"]
    assert result["after_reclaim"]["emitted"] == 1


def test_finalizing_a_job_twice_returns_the_same_result_and_emits_it_once(db) -> None:
    result = _run(db, '''
async def main():
    job_id = await submit()
    pipeline = await pipeline_service.get_pipeline("org_a", PIPE)
    policy = await quality_service.get_policy("org_a", POLICY_ID)
    progress = {"chunks_completed": 1, "failed": 1, "total": 4, "samples": []}
    first = await quality_service.finalize_job_result(org_id="org_a", pipeline=pipeline, policy=policy, progress=progress, job_id=job_id)
    second = await quality_service.finalize_job_result(org_id="org_a", pipeline=pipeline, policy=policy, progress=progress, job_id=job_id)
    return {"same": first["id"] == second["id"], "stored": len(await results_of(job_id)), "emitted": len(emitted)}
''')
    assert result == {"same": True, "stored": 1, "emitted": 1}


def test_two_workers_finalizing_the_same_job_at_once_produce_one_result(db) -> None:
    result = _run(db, '''
async def main():
    job_id = await submit()
    pipeline = await pipeline_service.get_pipeline("org_a", PIPE)
    policy = await quality_service.get_policy("org_a", POLICY_ID)
    progress = {"chunks_completed": 1, "failed": 1, "total": 4, "samples": []}
    results = await asyncio.gather(*[quality_service.finalize_job_result(org_id="org_a", pipeline=pipeline, policy=policy, progress=progress, job_id=job_id)
                                     for _ in range(6)])
    return {"distinct_ids": len({r["id"] for r in results}), "stored": len(await results_of(job_id)), "emitted": len(emitted)}
''')
    assert result == {"distinct_ids": 1, "stored": 1, "emitted": 1}


def test_different_jobs_each_get_their_own_result(db) -> None:
    result = _run(db, '''
async def main():
    pipeline = await pipeline_service.get_pipeline("org_a", PIPE)
    policy = await quality_service.get_policy("org_a", POLICY_ID)
    ids = []
    for _ in range(3):
        job_id = await submit()
        r = await quality_service.finalize_job_result(org_id="org_a", pipeline=pipeline, policy=policy,
                                                      progress={"chunks_completed": 1, "failed": 0, "total": 4, "samples": []}, job_id=job_id)
        ids.append(r["id"])
    return {"distinct": len(set(ids)), "stored": (await rows("SELECT count(*) FROM dc_quality_results"))[0][0]}
''')
    assert result == {"distinct": 3, "stored": 3}


def test_results_that_did_not_come_from_a_job_are_unaffected(db) -> None:
    """A synchronous preview or execute has no job: many of them can exist, each with job_id NULL."""
    result = _run(db, '''
async def main():
    pipeline = await pipeline_service.get_pipeline("org_a", PIPE)
    policy = await quality_service.get_policy("org_a", POLICY_ID)
    for _ in range(3):
        await quality_service._persist_result("org_a", policy, {
            "org_id": "org_a", "pipeline_id": PIPE, "quality_policy_id": POLICY_ID, "topic_name": "t", "rule_name": "*", "severity": "error",
            "status": "passed", "failed_count": 0, "total_count": 4, "sample_failures": []})
    return {"rows": (await rows("SELECT count(*), count(job_id) FROM dc_quality_results"))[0]}
''')
    assert result == {"rows": [3, 0]}


def test_the_database_itself_refuses_a_second_result_for_a_job(db) -> None:
    """Defense in depth: even a hand-written INSERT cannot create two results for one job."""
    db.execute(f"INSERT INTO dc_quality_results (id, org_id, status, failed_count, total_count, sample_failures, job_id) VALUES (gen_random_uuid(), 'org_a', 'passed', 0, 0, '[]', '{POLICY_ID}')")
    with pytest.raises(Exception, match="uq_dc_quality_results_job_id"):
        db.execute(f"INSERT INTO dc_quality_results (id, org_id, status, failed_count, total_count, sample_failures, job_id) VALUES (gen_random_uuid(), 'org_a', 'passed', 0, 0, '[]', '{POLICY_ID}')")
