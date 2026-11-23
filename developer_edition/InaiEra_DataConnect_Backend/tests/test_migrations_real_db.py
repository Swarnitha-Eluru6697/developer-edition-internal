"""
Hardening, against a REAL PostgreSQL: migrations from the previous release, and a virgin database.

Each test creates its OWN scratch database on the server named by DATACONNECT_DATABASE_URL and drops it afterwards — the real database is
never touched. Skipped unless DATACONNECT_REAL_DB_TESTS=1 (the role needs CREATEDB):

    DATACONNECT_DATABASE_URL=postgresql+asyncpg://dataconnect:dataconnect@localhost:5435/dataconnect \\
    DATACONNECT_REAL_DB_TESTS=1 .venv/bin/python -m pytest tests/test_migrations_real_db.py --no-cov

What "previous release" means here: revision PREVIOUS_RELEASE, the head before the 0044-0049 hardening migrations. There are no release tags, so it
is a named constant — move it forward when a release is cut.

Why the previous release is built by downgrading, not by migrating from 0001: the chain is a series of DELTAS written against a database
`Base.metadata.create_all` already built. Run from nothing it fails at 0042 (and, with that fixed, would produce a schema that differs from the
models in 142 ways — UUID instead of VARCHAR template ids, missing indexes, different nullability). So a virgin database is created from the
models and stamped at head (app/startup.py), and the previous-release state is that schema downgraded to PREVIOUS_RELEASE — which exercises the
downgrades as well as the upgrades.
"""

import os

import pytest
from scratch_db import Scratch, scratch_database

pytestmark = pytest.mark.skipif(not os.environ.get("DATACONNECT_REAL_DB_TESTS"), reason="needs a real PostgreSQL (DATACONNECT_REAL_DB_TESTS=1)")

PREVIOUS_RELEASE = "0043_topic_separator"
HEAD = "0056_pipeline_record_format"
ORG = "org_mig"
OPERATION_ROWS = {   # (status, has_result) -> what result_status must become
    ("succeeded", True): "valid", ("succeeded", False): "invalid", ("failed", False): None, ("running", False): None, ("queued", False): None,
}


@pytest.fixture
def scratch():
    with scratch_database("dc_migtest") as db:
        yield db


def _seed_previous_release(db: Scratch, *, dotted_topic: bool = False) -> dict:
    """Realistic rows in the 0043 schema: an org, a template, a connection, a source and a sink pipeline, runtime state, operations of every
    status, a quality policy and a job."""
    topic = f"{ORG}.raw.d.s.e.batch.v1" if dotted_topic else "raw-d-s-e-batch-v1"
    separator = "." if dotted_topic else "-"
    db.execute(f"""
        INSERT INTO dc_organizations (org_id, name, status) VALUES ('{ORG}', 'Migration test', 'active');
        INSERT INTO dc_connector_templates (id, name, connector_type, category, connector_class, runtime_pool_type, config_schema, default_config,
            default_transforms, version, is_active, supported_transforms, supported_kafka_connect_versions, supported_source_versions,
            transform_compatibility, supported_modes, supports_dlq, supports_schema_discovery)
          VALUES ('tpl_mig', 'Mig', 'source', 'database', 'x.Y', 'connect-jdbc-pool', '{{}}', '{{}}', '[]', '1', true, '[]', '[]', '[]', '{{}}', '[]', false, false);
        INSERT INTO dc_connection_profiles (id, org_id, name, source_type, connection_config)
          VALUES ('00000000-0000-0000-0000-0000000000c1', '{ORG}', 'conn', 'postgresql', '{{"hostname": "h"}}');
        INSERT INTO dc_pipelines (id, org_id, name, template_id, connection_profile_id, zone, domain, source_system, entity, mode, version, direction,
            connector_name, connector_config, generated_config, template_snapshot, status, error_handling_policy, runtime_pool_id, topic_name, topic_separator)
          VALUES ('00000000-0000-0000-0000-0000000000a1', '{ORG}', 'src', 'tpl_mig', '00000000-0000-0000-0000-0000000000c1', 'raw', 'd', 's', 'e',
            'batch', 'v1', 'SOURCE', 'conn-src', '{{"tables": ["t"]}}', '{{}}', '{{}}', 'running', 'FAIL_FAST', 'connect-jdbc-pool', '{topic}', '{separator}');
        INSERT INTO dc_pipeline_runtime_state (id, org_id, pipeline_id, connector_name, desired_state, desired_generation, observed_state, observed_generation)
          VALUES ('00000000-0000-0000-0000-0000000000b1', '{ORG}', '00000000-0000-0000-0000-0000000000a1', 'conn-src', 'running', 3, 'running', 3);
        INSERT INTO dc_quality_policies (id, org_id, name, rules, on_failure, quarantine_sink)
          VALUES ('00000000-0000-0000-0000-0000000000d1', '{ORG}', 'pol', '[]', 'route_to_quarantine', 'kafka');
        INSERT INTO dc_quality_execution_jobs (id, org_id, pipeline_id, quality_policy_id, status, record_count)
          VALUES ('00000000-0000-0000-0000-0000000000e1', '{ORG}', '00000000-0000-0000-0000-0000000000a1', '00000000-0000-0000-0000-0000000000d1', 'failed', 5);
    """)
    if not dotted_topic:
        db.execute(f"""
            INSERT INTO dc_pipelines (id, org_id, name, template_id, connection_profile_id, zone, domain, source_system, entity, mode, version, direction,
                connector_name, connector_config, generated_config, template_snapshot, status, error_handling_policy, runtime_pool_id, topic_separator)
              VALUES ('00000000-0000-0000-0000-0000000000a2', '{ORG}', 'snk', 'tpl_mig', '00000000-0000-0000-0000-0000000000c1', 'prepared', 'd', 's', 'e2',
                'batch', 'v1', 'SINK', 'conn-snk', '{{"topics": "{topic}"}}', '{{}}', '{{}}', 'running', 'DLQ_AND_CONTINUE', 'connect-sink-pool', '-');
        """)
    for index, ((status, has_result), _) in enumerate(OPERATION_ROWS.items()):
        result = "'{\"ok\": true}'::jsonb" if has_result else "NULL"
        db.execute(f"INSERT INTO dc_operations (id, org_id, operation_type, resource_type, resource_id, status, result) "
                   f"VALUES ('00000000-0000-0000-0000-00000000f0{index:02d}', '{ORG}', 'pipeline_deploy', 'pipeline', 'p', '{status}', {result})")
    return {"topic": topic}


def _virgin_to_previous_release(db: Scratch) -> None:
    result = db.startup_migrations()
    assert result.returncode == 0, result.stderr[-1500:]
    assert db.version() == HEAD
    db.alembic("downgrade", PREVIOUS_RELEASE)
    assert db.version() == PREVIOUS_RELEASE


# ── a virgin database ──────────────────────────────────────────────────────────────────────────────────────────────

def test_a_virgin_database_is_created_from_the_models_and_stamped_at_head(scratch) -> None:
    result = scratch.startup_migrations()

    assert result.returncode == 0, result.stderr[-1500:]
    assert scratch.version() == HEAD
    assert scratch.scalar("SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public' AND table_name LIKE 'dc\\_%'") > 20
    assert scratch.scalar("SELECT data_type FROM information_schema.columns WHERE table_name = 'dc_pipelines' AND column_name = 'template_id'") == "character varying"
    assert scratch.drift() == []                           # the schema IS the models


def test_starting_again_on_an_initialised_database_changes_nothing(scratch) -> None:
    assert scratch.startup_migrations().returncode == 0
    scratch.execute(f"INSERT INTO dc_organizations (org_id, name, status) VALUES ('{ORG}', 'kept', 'active')")

    second = scratch.startup_migrations()

    assert second.returncode == 0, second.stderr[-1500:]
    assert scratch.version() == HEAD
    assert scratch.scalar(f"SELECT name FROM dc_organizations WHERE org_id = '{ORG}'") == "kept"   # not recreated, not wiped


def test_the_chain_still_refuses_to_run_from_nothing_so_a_virgin_database_never_gets_a_wrong_schema(scratch) -> None:
    """Guards the REASON for the bootstrap: if someone makes `alembic upgrade head` succeed on an empty database without making its schema
    equal the models, a fresh deployment would silently get UUID template ids and no indexes. When the chain is repaired, this test is the one
    to replace with a drift == [] assertion."""
    result = scratch.alembic("upgrade", "head", check=False)
    assert result.returncode != 0
    assert scratch.version() in (None, "0001_initial")     # it did not complete; there is no half-migrated "head"


# ── the previous release ───────────────────────────────────────────────────────────────────────────────────────────

def test_the_previous_release_upgrades_to_head_and_keeps_its_data(scratch) -> None:
    _virgin_to_previous_release(scratch)
    seeded = _seed_previous_release(scratch)
    before = {
        "orgs": scratch.scalar("SELECT count(*) FROM dc_organizations"),
        "pipelines": scratch.query("SELECT id::text, name, topic_name, connector_config::text FROM dc_pipelines ORDER BY name"),
        "state": scratch.query("SELECT pipeline_id, desired_generation, observed_generation FROM dc_pipeline_runtime_state"),
        "jobs": scratch.query("SELECT id::text, status, record_count FROM dc_quality_execution_jobs"),
    }

    scratch.alembic("upgrade", "head")

    assert scratch.version() == HEAD
    assert scratch.scalar("SELECT count(*) FROM dc_organizations") == before["orgs"]
    assert [tuple(r) for r in scratch.query("SELECT id::text, name, topic_name, connector_config::text FROM dc_pipelines ORDER BY name")] == [tuple(r) for r in before["pipelines"]]
    assert [tuple(r) for r in scratch.query("SELECT pipeline_id, desired_generation, observed_generation FROM dc_pipeline_runtime_state")] == [tuple(r) for r in before["state"]]
    assert [tuple(r) for r in scratch.query("SELECT id::text, status, record_count FROM dc_quality_execution_jobs")] == [tuple(r) for r in before["jobs"]]
    assert scratch.scalar("SELECT topic_name FROM dc_pipelines WHERE name = 'src'") == seeded["topic"]
    assert scratch.scalar("SELECT connector_config ->> 'topics' FROM dc_pipelines WHERE name = 'snk'") == seeded["topic"]
    # 0056: every pipeline that existed keeps writing as before (v1); a pipeline created afterwards is v2.
    assert [r[0] for r in scratch.query("SELECT DISTINCT record_format FROM dc_pipelines")] == ["v1"]
    assert scratch.scalar("SELECT column_default FROM information_schema.columns WHERE table_name = 'dc_pipelines' "
                          "AND column_name = 'record_format'").startswith("'v2'")


def test_the_columns_and_backfills_the_new_releases_added_arrive_correctly(scratch) -> None:
    _virgin_to_previous_release(scratch)
    _seed_previous_release(scratch)

    scratch.alembic("upgrade", "head")

    # 0044: the per-pipeline separator column and its constraint are gone; the sink input rule is now dash-only
    assert scratch.scalar("SELECT count(*) FROM information_schema.columns WHERE table_name = 'dc_pipelines' AND column_name = 'topic_separator'") == 0
    assert scratch.scalar("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'ck_dc_pipelines_sink_input_topic'") is not None
    # 0045: nothing is "stuck" on an existing pipeline
    state = scratch.query("SELECT quality_blocked_since, quality_blocked_heartbeat_at FROM dc_pipeline_runtime_state")[0]
    assert (state[0], state[1]) == (None, None)
    # 0046: existing operations are classified from what they stored
    for index, ((status, has_result), expected) in enumerate(OPERATION_ROWS.items()):
        got = scratch.scalar(f"SELECT result_status FROM dc_operations WHERE id = '00000000-0000-0000-0000-00000000f0{index:02d}'")
        assert got == expected, f"{status}/result={has_result}: expected {expected!r}, got {got!r}"
    # 0047: an old job was not resubmitted from anything
    assert scratch.scalar("SELECT resubmitted_from FROM dc_quality_execution_jobs") is None
    # 0048: every existing pipeline keeps doing exactly what it did — observing — and ENFORCE needs a policy
    assert scratch.scalar("SELECT count(*) FROM dc_pipelines") > 0
    assert scratch.scalar("SELECT count(*) FROM dc_pipelines WHERE quality_mode <> 'OBSERVE'") == 0
    with pytest.raises(Exception, match="ck_dc_pipelines_quality_mode"):
        scratch.execute("UPDATE dc_pipelines SET quality_mode = 'BLOCK' WHERE name = 'src'")
    scratch.execute("UPDATE dc_pipelines SET quality_policy_id = NULL WHERE name = 'src'")
    with pytest.raises(Exception, match="ck_dc_pipelines_enforce_needs_policy"):
        scratch.execute("UPDATE dc_pipelines SET quality_mode = 'ENFORCE' WHERE name = 'src'")
    scratch.execute("UPDATE dc_pipelines SET quality_policy_id = (SELECT id FROM dc_quality_policies LIMIT 1), quality_mode = 'ENFORCE' WHERE name = 'src'")
    assert scratch.scalar("SELECT quality_mode FROM dc_pipelines WHERE name = 'src'") == "ENFORCE"
    # 0049/0050: the failure-count offsets table exists, starts empty, and is keyed by (kind, topic, partition) — the topic, not a pipeline
    assert scratch.scalar("SELECT count(*) FROM dc_failure_count_offsets") == 0
    scratch.execute(f"INSERT INTO dc_failure_count_offsets (org_id, kind, topic, kafka_partition, next_offset) VALUES ('{ORG}', 'ingestion', 't', 0, 5)")
    with pytest.raises(Exception, match="dc_failure_count_offsets_pkey"):
        scratch.execute(f"INSERT INTO dc_failure_count_offsets (org_id, kind, topic, kafka_partition, next_offset) VALUES ('{ORG}', 'ingestion', 't', 0, 6)")
    scratch.execute("DELETE FROM dc_failure_count_offsets")
    # 0051: the quality stream offsets table exists, starts empty, is keyed by (pipeline, topic, partition) and goes with its pipeline
    assert scratch.scalar("SELECT count(*) FROM dc_quality_stream_offsets") == 0
    scratch.execute("INSERT INTO dc_quality_stream_offsets (pipeline_id, topic, kafka_partition, next_offset) "
                    "SELECT id, 't', 0, 5 FROM dc_pipelines WHERE name = 'snk'")
    with pytest.raises(Exception, match="dc_quality_stream_offsets_pkey"):
        scratch.execute("INSERT INTO dc_quality_stream_offsets (pipeline_id, topic, kafka_partition, next_offset) "
                        "SELECT id, 't', 0, 6 FROM dc_pipelines WHERE name = 'snk'")
    scratch.execute("DELETE FROM dc_pipelines WHERE name = 'snk'")
    assert scratch.scalar("SELECT count(*) FROM dc_quality_stream_offsets") == 0                  # ON DELETE CASCADE
    # 0052: a quality result may name the job it summarises, at most one result per job; existing results have none
    assert scratch.scalar("SELECT count(job_id) FROM dc_quality_results") == 0
    # 0053: the dead-letter topic of a sink is owned by one pipeline: a second sink cannot take it, and one with no dead-letter policy can coexist
    assert scratch.scalar("SELECT count(*) FROM pg_indexes WHERE indexname = 'uq_dc_pipelines_dead_letter_topic'") == 1
    scratch.execute(_second_sink_on_the_same_dead_letter_queue("owner"))
    with pytest.raises(Exception, match="uq_dc_pipelines_dead_letter_topic"):
        scratch.execute(_second_sink_on_the_same_dead_letter_queue("late"))
    scratch.execute(_second_sink_on_the_same_dead_letter_queue("keeps-no-queue", policy="FAIL_FAST"))


def test_a_database_with_an_old_dotted_topic_is_refused_and_left_exactly_as_it_was(scratch) -> None:
    """0044 must not silently strand a running connector's topic. It refuses, and — DDL being transactional — changes nothing."""
    _virgin_to_previous_release(scratch)
    _seed_previous_release(scratch, dotted_topic=True)

    result = scratch.alembic("upgrade", "head", check=False)

    assert result.returncode != 0
    assert "'.'-separated topics" in result.stderr, result.stderr[-800:]       # 0044's own explanation
    assert "orphaned" in result.stderr
    assert scratch.version() == PREVIOUS_RELEASE                                  # still the previous release
    assert scratch.scalar("SELECT topic_name FROM dc_pipelines WHERE name = 'src'") == f"{ORG}.raw.d.s.e.batch.v1"
    assert scratch.scalar("SELECT count(*) FROM information_schema.columns WHERE table_name = 'dc_pipelines' AND column_name = 'topic_separator'") == 1


# ── reversibility ──────────────────────────────────────────────────────────────────────────────────────────────────

def test_every_migration_since_the_previous_release_downgrades_and_upgrades_again_without_losing_data(scratch) -> None:
    _virgin_to_previous_release(scratch)
    _seed_previous_release(scratch)
    scratch.alembic("upgrade", "head")
    stable = ("SELECT p.id::text, p.name, p.topic_name, o.status FROM dc_pipelines p, dc_operations o "
              "WHERE o.id = '00000000-0000-0000-0000-00000000f000' ORDER BY p.name")
    snapshot = [tuple(r) for r in scratch.query(stable)]

    step = 0
    while scratch.version() != PREVIOUS_RELEASE:           # one step at a time, head -> ... -> the previous release (bounded below)
        step += 1
        assert step <= 50, f"never reached {PREVIOUS_RELEASE}"
        scratch.alembic("downgrade", "-1")
        assert [tuple(r) for r in scratch.query(stable)] == snapshot, f"data changed after downgrade step {step}"
    scratch.alembic("upgrade", "head")

    assert scratch.version() == HEAD
    assert [tuple(r) for r in scratch.query(stable)] == snapshot


def test_after_going_down_to_the_previous_release_and_back_the_schema_equals_the_models(scratch) -> None:
    """The migrations since the previous release leave exactly the schema the models describe — nothing missing, nothing extra."""
    _virgin_to_previous_release(scratch)
    scratch.alembic("upgrade", "head")

    assert scratch.drift() == []


def test_0050_turns_per_pipeline_offsets_into_per_topic_offsets_keeping_the_highest(scratch) -> None:
    """0049 kept the counted offsets per pipeline; with a topic shared by several pipelines they belong to the topic. Existing rows must not lose
    progress (a lower offset would make the counter count records again) and must not duplicate."""
    _virgin_to_previous_release(scratch)
    _seed_previous_release(scratch)
    scratch.alembic("upgrade", "0049_failure_count_offsets")
    src = scratch.scalar("SELECT id::text FROM dc_pipelines WHERE name = 'src'")
    snk = scratch.scalar("SELECT id::text FROM dc_pipelines WHERE name = 'snk'")
    for pipeline, partition, offset in ((src, 0, 5), (snk, 0, 9), (src, 1, 4), (snk, 2, 7)):
        scratch.execute(f"INSERT INTO dc_failure_count_offsets (pipeline_id, kind, topic, kafka_partition, next_offset) "
                        f"VALUES ('{pipeline}', 'ingestion', 'shared-dlq', {partition}, {offset})")

    scratch.alembic("upgrade", "head")

    rows = [tuple(r) for r in scratch.query("SELECT org_id, kind, topic, kafka_partition, next_offset FROM dc_failure_count_offsets ORDER BY kafka_partition")]
    assert rows == [(ORG, "ingestion", "shared-dlq", 0, 9),          # the higher of 5 and 9
                    (ORG, "ingestion", "shared-dlq", 1, 4),
                    (ORG, "ingestion", "shared-dlq", 2, 7)]
    assert scratch.scalar("SELECT count(*) FROM information_schema.columns WHERE table_name = 'dc_failure_count_offsets' AND column_name = 'pipeline_id'") == 0


def _second_sink_on_the_same_dead_letter_queue(name: str, policy: str = "DLQ_AND_CONTINUE") -> str:
    """A second sink with the seeded sink's domain / source / entity / version (so the same dead-letter topic), differing only in its mode."""
    return f"""
        INSERT INTO dc_pipelines (id, org_id, name, template_id, connection_profile_id, zone, domain, source_system, entity, mode, version, direction,
            connector_name, connector_config, generated_config, template_snapshot, status, error_handling_policy, runtime_pool_id)
          VALUES (gen_random_uuid(), '{ORG}', '{name}', 'tpl_mig', '00000000-0000-0000-0000-0000000000c1', 'prepared', 'd', 's', 'e2', 'stream', 'v1',
            'SINK', 'conn-{name}', '{{"topics": "raw-d-s-e-batch-v1"}}', '{{}}', '{{}}', 'running', '{policy}', 'connect-sink-pool');
    """


def test_0053_refuses_to_run_while_two_sinks_already_share_a_dead_letter_queue_and_changes_nothing(scratch) -> None:
    """A migration must not choose which running connector keeps its dead-letter queue. It names the pipelines and leaves the database as it was."""
    _virgin_to_previous_release(scratch)
    _seed_previous_release(scratch)
    scratch.alembic("upgrade", "0052_quality_result_job_id")
    scratch.execute(_second_sink_on_the_same_dead_letter_queue("shared-with-snk"))

    result = scratch.alembic("upgrade", "head", check=False)

    assert result.returncode != 0
    assert "dead-letter queue(s) are shared" in result.stderr
    assert "snk" in result.stderr
    assert "shared-with-snk" in result.stderr
    assert "d/s/e2/v1" in result.stderr                                           # and the identity they share
    assert scratch.version() == "0052_quality_result_job_id"                      # nothing was applied
    assert scratch.scalar("SELECT count(*) FROM pg_indexes WHERE indexname = 'uq_dc_pipelines_dead_letter_topic'") == 0


def test_0053_runs_once_the_shared_queue_is_resolved_whichever_way(scratch) -> None:
    _virgin_to_previous_release(scratch)
    _seed_previous_release(scratch)
    scratch.alembic("upgrade", "0052_quality_result_job_id")
    scratch.execute(_second_sink_on_the_same_dead_letter_queue("shared-with-snk"))
    assert scratch.alembic("upgrade", "head", check=False).returncode != 0

    scratch.execute("UPDATE dc_pipelines SET error_handling_policy = 'FAIL_FAST' WHERE name = 'shared-with-snk'")      # it keeps no queue now

    scratch.alembic("upgrade", "head")
    assert scratch.version() == HEAD
    assert scratch.scalar("SELECT count(*) FROM pg_indexes WHERE indexname = 'uq_dc_pipelines_dead_letter_topic'") == 1


def test_0053_does_not_count_sources_or_sinks_without_a_dead_letter_policy_as_sharing(scratch) -> None:
    _virgin_to_previous_release(scratch)
    _seed_previous_release(scratch)
    scratch.alembic("upgrade", "0052_quality_result_job_id")
    scratch.execute(_second_sink_on_the_same_dead_letter_queue("keeps-no-queue", policy="SKIP_AND_LOG"))

    scratch.alembic("upgrade", "head")

    assert scratch.version() == HEAD
