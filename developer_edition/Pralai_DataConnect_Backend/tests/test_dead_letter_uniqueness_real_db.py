"""
NEW-08, against a REAL PostgreSQL: two sink pipelines cannot own the same connector dead-letter topic, even when their creates (or edits) run at the
same moment.

A sink's dead-letter topic is named from domain / source system / entity / version only, so two sinks that differ only in zone or mode share it. The API
refuses the second up front, but that check and the insert are separate steps, so two concurrent creates could both pass the check and both be stored.
A partial unique index on the sink's dead-letter identity now makes the database refuse whichever commits second, and the loser gets the SAME validation
error a sequential conflict gets.

The race is forced deterministically: every concurrent call is held at a barrier until ALL of them have passed validation, then they write together.
Each test runs the app code in a subprocess against a scratch database (tests/scratch_db.py). Skipped unless DATACONNECT_REAL_DB_TESTS=1.
"""

import json
import os
import subprocess
import sys

import pytest
from scratch_db import BACKEND, scratch_database

pytestmark = pytest.mark.skipif(not os.environ.get("DATACONNECT_REAL_DB_TESTS"), reason="needs a real PostgreSQL (DATACONNECT_REAL_DB_TESTS=1)")

PRELUDE = '''
import asyncio, json
from sqlalchemy import text
from app.core.errors import ValidationError
from app.db.session import AsyncSessionLocal
from app.repositories.dataconnect import PipelineRepository
from app.schemas.pipeline import PipelineCreate, PipelineUpdate
from app.services.pipeline_service import pipeline_service

CONN = "00000000-0000-0000-0000-0000000000a1"
IN_TOPIC = "raw-d-s-in-batch-v1"

def sink(mode, *, org="org_a", entity="e", policy="DLQ_AND_CONTINUE", name=None, zone="raw", domain="d"):
    return PipelineCreate(name=name or f"{org}-{domain}-{entity}-{mode}-{zone}", template_id="tpl_sink", connection_profile_id=CONN, zone=zone,
        domain=domain, source_system="s", entity=entity, mode=mode, version="v1", connector_config={"topics": IN_TOPIC.replace("org_a", org)},
        error_handling_policy=policy)

def source(mode):
    return PipelineCreate(name=f"source-{mode}", template_id="tpl_source", connection_profile_id=CONN, zone="raw", domain="d", source_system="s",
        entity="e", mode=mode, version="v1", connector_config={"tables": ["t"]})

def hold_writes_until(n):
    """Hold every insert/update at a barrier until n of them are waiting, so all have passed validation before any writes."""
    barrier = asyncio.Barrier(n)
    real_create, real_update = PipelineRepository.create, PipelineRepository.update
    async def create(self, row):
        await barrier.wait()
        return await real_create(self, row)
    async def update(self, row, fields):
        if "error_handling_policy" in fields:
            await barrier.wait()
        return await real_update(self, row, fields)
    PipelineRepository.create, PipelineRepository.update = create, update
    return lambda: (setattr(PipelineRepository, "create", real_create), setattr(PipelineRepository, "update", real_update))

def label(r):
    return "created" if isinstance(r, dict) else type(r).__name__

async def rows(sql):
    async with AsyncSessionLocal() as s:
        return [tuple(r) for r in (await s.execute(text(sql))).all()]
'''


@pytest.fixture
def db():
    with scratch_database("dc_dlqunique") as scratch:
        assert scratch.startup_migrations().returncode == 0
        scratch.execute("""
            INSERT INTO dc_organizations (org_id, name, status) VALUES ('org_a', 'Org A', 'active'), ('org_b', 'Org B', 'active');
            INSERT INTO dc_connector_templates (id, name, connector_type, category, connector_class, runtime_pool_type, config_schema, default_config,
                default_transforms, version, is_active, supported_transforms, supported_kafka_connect_versions, supported_source_versions,
                transform_compatibility, supported_modes, supports_dlq, supports_schema_discovery)
              VALUES ('tpl_sink', 'S', 'sink', 'database', 'io.confluent.connect.jdbc.JdbcSinkConnector', 'connect-sink-pool', '{}', '{}', '[]', '1', true, '[]', '[]', '[]', '{}', '[]', true, false),
                     ('tpl_source', 'R', 'source', 'database', 'io.confluent.connect.jdbc.JdbcSourceConnector', 'connect-jdbc-pool', '{}', '{}', '[]', '1', true, '[]', '[]', '[]', '{}', '[]', false, false);
            INSERT INTO dc_connection_profiles (id, org_id, name, source_type, connection_config)
              VALUES ('00000000-0000-0000-0000-0000000000a1', 'org_a', 'conn', 'postgresql', '{}'),
                     ('00000000-0000-0000-0000-0000000000b1', 'org_b', 'conn', 'postgresql', '{}');
        """)
        yield scratch


def _run(scratch, body: str, timeout: int = 180):
    code = PRELUDE + "\n" + body + "\nprint('RESULT=' + json.dumps(asyncio.run(main()), default=str))\n"
    result = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, env=scratch.env(), capture_output=True, text=True, timeout=timeout)
    assert result.returncode == 0, result.stderr[-2500:]
    line = next(ln for ln in reversed(result.stdout.splitlines()) if ln.startswith("RESULT="))
    return json.loads(line[len("RESULT="):])


# ── The review's check ─────────────────────────────────────────────────────────────────────────────────────────────────

def test_two_concurrent_conflicting_creates_let_exactly_one_succeed(db) -> None:
    result = _run(db, '''
async def main():
    restore = hold_writes_until(2)
    results = await asyncio.gather(pipeline_service.create_pipeline("org_a", sink("batch")),
                                   pipeline_service.create_pipeline("org_a", sink("stream")), return_exceptions=True)
    restore()
    return {"outcomes": sorted(label(r) for r in results), "stored": len(await rows("SELECT 1 FROM dc_pipelines"))}
''')
    assert result == {"outcomes": ["ValidationError", "created"], "stored": 1}


def test_the_loser_of_the_race_gets_the_same_error_as_a_sequential_loser_and_is_told_who_owns_the_queue(db) -> None:
    result = _run(db, '''
async def main():
    restore = hold_writes_until(2)
    results = await asyncio.gather(pipeline_service.create_pipeline("org_a", sink("batch", name="first")),
                                   pipeline_service.create_pipeline("org_a", sink("stream", name="second")), return_exceptions=True)
    restore()
    loser = next(r for r in results if isinstance(r, ValidationError))
    winner = next(r for r in results if isinstance(r, dict))["name"]
    try:                                                              # the SAME conflict, now sequentially
        await pipeline_service.create_pipeline("org_a", sink("replay", name="third"))
        sequential = None
    except ValidationError as exc:
        sequential = exc
    return {"winner": winner, "race_message": str(loser), "race_fields": [f.field for f in loser.field_errors],
            "sequential_message": str(sequential), "sequential_fields": [f.field for f in sequential.field_errors]}
''')
    assert f"{result['winner']!r}" in result["race_message"]              # names the pipeline that owns the dead-letter queue
    assert "dead-letter queue" in result["race_message"]
    assert result["race_fields"] == result["sequential_fields"] == ["entity"]
    assert result["race_message"].replace(repr(result["winner"]), "X") == result["sequential_message"].replace(repr(result["winner"]), "X")


@pytest.mark.parametrize("racers", [3, 6])
def test_many_concurrent_creates_for_one_dead_letter_queue_leave_exactly_one(db, racers) -> None:
    modes = ["batch", "stream", "replay", "cdc", "snapshot", "api"][:racers]
    result = _run(db, f'''
async def main():
    modes = {modes!r}
    restore = hold_writes_until(len(modes))
    results = await asyncio.gather(*[pipeline_service.create_pipeline("org_a", sink(m)) for m in modes], return_exceptions=True)
    restore()
    return {{"outcomes": sorted(label(r) for r in results), "stored": len(await rows("SELECT 1 FROM dc_pipelines"))}}
''')
    assert result == {"outcomes": ["ValidationError"] * (racers - 1) + ["created"], "stored": 1}


# ── What must NOT be blocked ───────────────────────────────────────────────────────────────────────────────────────────

def test_concurrent_creates_for_different_dead_letter_queues_all_succeed(db) -> None:
    result = _run(db, '''
async def main():
    restore = hold_writes_until(3)
    results = await asyncio.gather(*[pipeline_service.create_pipeline("org_a", sink("batch", entity=e)) for e in ("e1", "e2", "e3")], return_exceptions=True)
    restore()
    return [label(r) for r in results]
''')
    assert result == ["created"] * 3


def test_the_same_queue_name_in_two_organizations_is_fine(db) -> None:
    result = _run(db, '''
async def main():
    # org_b's connection is its own; the identity (domain/source/entity/version) is identical to org_a's
    a = await pipeline_service.create_pipeline("org_a", sink("batch"))
    b = PipelineCreate(name="b", template_id="tpl_sink", connection_profile_id="00000000-0000-0000-0000-0000000000b1", zone="raw", domain="d",
                       source_system="s", entity="e", mode="batch", version="v1", connector_config={"topics": "raw-d-s-in-batch-v1"},
                       error_handling_policy="DLQ_AND_CONTINUE")
    return [label(a), label(await pipeline_service.create_pipeline("org_b", b))]
''')
    assert result == ["created", "created"]


def test_a_sink_without_a_dead_letter_policy_does_not_own_the_queue(db) -> None:
    result = _run(db, '''
async def main():
    a = await pipeline_service.create_pipeline("org_a", sink("batch", policy="DLQ_AND_CONTINUE"))
    b = await pipeline_service.create_pipeline("org_a", sink("stream", policy="FAIL_FAST"))        # keeps no dead-letter topic
    return [label(a), label(b)]
''')
    assert result == ["created", "created"]


def test_sources_never_conflict_with_each_other_or_with_a_sink(db) -> None:
    result = _run(db, '''
async def main():
    out = [label(await pipeline_service.create_pipeline("org_a", sink("batch")))]
    for mode in ("cdc", "snapshot"):
        out.append(label(await pipeline_service.create_pipeline("org_a", source(mode))))
    return out
''')
    assert result == ["created"] * 3


def test_a_sequential_conflict_is_still_refused_up_front(db) -> None:
    result = _run(db, '''
async def main():
    await pipeline_service.create_pipeline("org_a", sink("batch"))
    try:
        await pipeline_service.create_pipeline("org_a", sink("stream"))
        return "created"
    except ValidationError as exc:
        return str(exc)[:60]
''')
    assert result.startswith("Pipeline ")


# ── Edits race the same way ────────────────────────────────────────────────────────────────────────────────────────────

def test_two_concurrent_edits_that_would_share_a_queue_let_exactly_one_succeed(db) -> None:
    """Two sinks sharing an identity are both FAIL_FAST (no queue yet). Switching both to a dead-letter policy at once must let only one."""
    result = _run(db, '''
async def main():
    a = await pipeline_service.create_pipeline("org_a", sink("batch", policy="FAIL_FAST"))
    b = await pipeline_service.create_pipeline("org_a", sink("stream", policy="FAIL_FAST"))
    restore = hold_writes_until(2)
    results = await asyncio.gather(pipeline_service.update_pipeline("org_a", a["id"], PipelineUpdate(error_handling_policy="DLQ_AND_CONTINUE")),
                                   pipeline_service.update_pipeline("org_a", b["id"], PipelineUpdate(error_handling_policy="DLQ_AND_CONTINUE")),
                                   return_exceptions=True)
    restore()
    policies = sorted(p for (p,) in await rows("SELECT error_handling_policy FROM dc_pipelines"))
    return {"outcomes": sorted(label(r) for r in results), "policies": policies}
''')
    assert result["outcomes"] == ["ValidationError", "created"]                # (the helper calls any returned pipeline "created")
    assert result["policies"] == ["DLQ_AND_CONTINUE", "FAIL_FAST"]            # the loser's row was left exactly as it was


def test_an_edit_that_conflicts_sequentially_is_refused_and_changes_nothing(db) -> None:
    result = _run(db, '''
async def main():
    await pipeline_service.create_pipeline("org_a", sink("batch"))
    b = await pipeline_service.create_pipeline("org_a", sink("stream", policy="FAIL_FAST"))
    try:
        await pipeline_service.update_pipeline("org_a", b["id"], PipelineUpdate(error_handling_policy="THRESHOLD"))
        outcome = "updated"
    except ValidationError:
        outcome = "ValidationError"
    return {"outcome": outcome, "policy": (await rows("SELECT error_handling_policy FROM dc_pipelines WHERE name = 'org_a-d-e-stream-raw'"))[0][0]}
''')
    assert result == {"outcome": "ValidationError", "policy": "FAIL_FAST"}


# ── The queue is released ──────────────────────────────────────────────────────────────────────────────────────────────

def test_deleting_the_owner_frees_the_queue(db) -> None:
    result = _run(db, '''
async def main():
    a = await pipeline_service.create_pipeline("org_a", sink("batch"))
    await pipeline_service.hard_delete("org_a", a["id"])
    return label(await pipeline_service.create_pipeline("org_a", sink("stream")))
''')
    assert result == "created"


def test_giving_up_the_dead_letter_policy_frees_the_queue(db) -> None:
    result = _run(db, '''
async def main():
    a = await pipeline_service.create_pipeline("org_a", sink("batch"))
    await pipeline_service.update_pipeline("org_a", a["id"], PipelineUpdate(error_handling_policy="FAIL_FAST"))
    return label(await pipeline_service.create_pipeline("org_a", sink("stream")))
''')
    assert result == "created"


# ── Defense in depth ───────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_database_itself_refuses_a_second_owner_even_when_the_api_is_bypassed(db) -> None:
    def insert(pid, name, mode, policy="DLQ_AND_CONTINUE"):
        return f"""INSERT INTO dc_pipelines (id, org_id, name, template_id, connection_profile_id, zone, domain, source_system, entity, mode, version, direction,
            connector_name, connector_config, generated_config, template_snapshot, status, error_handling_policy, runtime_pool_id)
          VALUES ('{pid}', 'org_a', '{name}', 'tpl_sink', '00000000-0000-0000-0000-0000000000a1', 'raw', 'd', 's', 'e', '{mode}', 'v1', 'SINK',
                  'c-{name}', '{{"topics": "raw-d-s-in-batch-v1"}}', '{{}}', '{{}}', 'draft', '{policy}', 'connect-sink-pool')"""

    db.execute(insert("00000000-0000-0000-0000-0000000000a2", "one", "batch"))
    with pytest.raises(Exception, match="uq_dc_pipelines_dead_letter_topic"):
        db.execute(insert("00000000-0000-0000-0000-0000000000a3", "two", "stream"))
    db.execute(insert("00000000-0000-0000-0000-0000000000a4", "three", "stream", policy="FAIL_FAST"))      # no queue: allowed


def test_the_index_never_applies_to_a_source_even_one_that_somehow_carries_a_dead_letter_policy(db) -> None:
    """Kafka Connect gives a source no dead-letter queue, so a source can never own one. The API refuses such a policy on a source; this checks that the
    DATABASE rule is about sinks only, so it cannot be tripped by rows that are not sinks."""
    def source_row(pid, name, mode):
        return f"""INSERT INTO dc_pipelines (id, org_id, name, template_id, connection_profile_id, zone, domain, source_system, entity, mode, version, direction,
            connector_name, connector_config, generated_config, template_snapshot, status, error_handling_policy, runtime_pool_id, topic_name)
          VALUES ('{pid}', 'org_a', '{name}', 'tpl_source', '00000000-0000-0000-0000-0000000000a1', 'raw', 'd', 's', 'e', '{mode}', 'v1', 'SOURCE',
                  'c-{name}', '{{"tables": ["t"]}}', '{{}}', '{{}}', 'draft', 'DLQ_AND_CONTINUE', 'connect-jdbc-pool', 'raw-d-s-e-{mode}-v1')"""

    db.execute(source_row("00000000-0000-0000-0000-0000000000a2", "one", "batch"))
    db.execute(source_row("00000000-0000-0000-0000-0000000000a3", "two", "stream"))              # same identity: allowed, they are not sinks
