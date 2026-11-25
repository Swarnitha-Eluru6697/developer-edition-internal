"""
The lineage graph shows exactly the flow a pipeline really has, against a REAL PostgreSQL.

It used to draw a "Quality Processor" (and, once, a prepared topic) for EVERY source pipeline, whether or not it had a quality policy, and it never removed
anything: get-or-create only ever added. Now:
  - no quality policy        -> source -> connector -> raw topic, nothing after it;
  - a quality policy         -> raw topic -> the policy -> the quarantine topic (when it routes failures there);
  - the policy ENFORCEd      -> and the prepared topic too (in OBSERVE nothing is written to it);
  - the policy removed, or ENFORCE switched back to OBSERVE -> the old processor / prepared / quarantine nodes and edges are removed.
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
from app.db.session import AsyncSessionLocal
from app.services.lineage_service import lineage_service

ORG = "org_a"
POLICY = "00000000-0000-0000-0000-0000000000c1"
LOG_POLICY = "00000000-0000-0000-0000-0000000000c2"

def uid(n):
    return f"00000000-0000-0000-0000-{n:012d}"

def pipeline(pid=1, *, mode="cdc", policy=None, quality_mode="OBSERVE", domain="d", entity="e", version="v1", zone="raw"):
    return {"id": uid(pid), "org_id": ORG, "name": f"pipe-{pid}", "domain": domain, "source_system": "s", "entity": entity, "version": version, "zone": zone,
            "mode": mode, "direction": "SOURCE", "topic_name": f"{ORG}-{zone}-{domain}-s-{entity}-{mode}-{version}",
            "connector_name": f"{domain}--s--{entity}--{mode}--{version}", "runtime_pool_id": "connect-cdc-pool",
            "quality_policy_id": policy, "quality_mode": quality_mode}

CONN = {"name": "Postgres"}

async def graph(pid=1):
    g = await lineage_service.graph_for_pipeline(ORG, uid(pid))
    names = {n["id"]: n for n in g["nodes"]}
    return {"nodes": sorted((n["asset_type"], n["name"]) for n in g["nodes"]),
            "edges": sorted((names[e["from_asset_id"]]["asset_type"], e["edge_type"], names[e["to_asset_id"]]["asset_type"]) for e in g["edges"])}

async def count(sql):
    async with AsyncSessionLocal() as s:
        return (await s.execute(text(sql))).scalar_one()
'''

RAW = "raw-d-s-e-cdc-v1"
QUARANTINE = "quarantine-d-s-e-validation-v1"
PREPARED = "prepared-d-s-e-cdc-v1"


@pytest.fixture
def db():
    with scratch_database("dc_lineageflow") as scratch:
        assert scratch.startup_migrations().returncode == 0
        scratch.execute("""
            INSERT INTO dc_organizations (org_id, name, status) VALUES ('org_a', 'Org A', 'active');
            INSERT INTO dc_quality_policies (id, org_id, name, rules, on_failure, quarantine_sink) VALUES
              ('00000000-0000-0000-0000-0000000000c1', 'org_a', 'Customer checks', '[]', 'route_to_quarantine', 'kafka'),
              ('00000000-0000-0000-0000-0000000000c2', 'org_a', 'Log only', '[]', 'log', 'kafka');
            INSERT INTO dc_connector_templates (id, name, connector_type, category, connector_class, runtime_pool_type, config_schema, default_config,
                default_transforms, version, is_active, supported_transforms, supported_kafka_connect_versions, supported_source_versions,
                transform_compatibility, supported_modes, supports_dlq, supports_schema_discovery)
              VALUES ('tpl_a', 'T', 'source', 'database', 'x.Y', 'connect-cdc-pool', '{}', '{}', '[]', '1', true, '[]', '[]', '[]', '{}', '[]', false, false);
            INSERT INTO dc_connection_profiles (id, org_id, name, source_type, connection_config)
              VALUES ('00000000-0000-0000-0000-0000000000a1', 'org_a', 'conn', 'postgresql', '{}');
        """)
        # Lineage edges belong to a pipeline row, so the pipelines the tests draw need to exist (their identity fields here are not what is drawn).
        for n in (1, 11, 12):
            scratch.execute(f"""
                INSERT INTO dc_pipelines (id, org_id, name, template_id, connection_profile_id, zone, domain, source_system, entity, mode, version, direction,
                    connector_name, topic_name, connector_config, generated_config, template_snapshot, status, error_handling_policy, runtime_pool_id)
                  VALUES ('00000000-0000-0000-0000-{n:012d}', 'org_a', 'row-{n}', 'tpl_a', '00000000-0000-0000-0000-0000000000a1', 'raw', 'rowdom', 's', 'row{n}',
                          'cdc', 'v1', 'SOURCE', 'row-connector-{n}', 'row-topic-{n}', '{{}}', '{{}}', '{{}}', 'running', 'SKIP_AND_LOG', 'connect-cdc-pool');
            """)
        yield scratch


def _run(scratch, body: str, timeout: int = 120):
    code = PRELUDE + "\n" + body + "\nprint('RESULT=' + json.dumps(asyncio.run(main()), default=str))\n"
    result = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, env=scratch.env(), capture_output=True, text=True, timeout=timeout)
    assert result.returncode == 0, result.stderr[-2500:]
    line = next(ln for ln in reversed(result.stdout.splitlines()) if ln.startswith("RESULT="))
    return json.loads(line[len("RESULT="):])


def test_a_pipeline_with_no_quality_policy_has_no_processor_no_quarantine_and_no_prepared_topic(db) -> None:
    out = _run(db, "async def main():\n    await lineage_service.materialize_pipeline_lineage(pipeline(), CONN)\n    return await graph()")
    assert out["nodes"] == [["connector", "d--s--e--cdc--v1"], ["kafka_topic", RAW], ["source", "Postgres"]]
    assert out["edges"] == [["connector", "INGESTS_TO", "kafka_topic"], ["source", "CONNECTS_TO", "connector"]]


def test_a_policy_adds_the_policy_and_its_quarantine_topic_after_the_raw_topic(db) -> None:
    out = _run(db, "async def main():\n    await lineage_service.materialize_pipeline_lineage(pipeline(policy=POLICY), CONN)\n    return await graph()")
    assert ["processor", "Customer checks (quality policy)"] in out["nodes"]
    assert ["kafka_topic", QUARANTINE] in out["nodes"]
    assert not any(n[1] == PREPARED for n in out["nodes"]), "OBSERVE writes nothing to a prepared topic, so none is drawn"
    assert ["kafka_topic", "VALIDATED_BY", "processor"] in out["edges"]
    assert ["processor", "QUARANTINES_TO", "kafka_topic"] in out["edges"]
    assert not any(e[1] == "EMITS_TO" for e in out["edges"])


def test_an_enforcing_policy_also_shows_the_prepared_topic(db) -> None:
    out = _run(db, "async def main():\n    await lineage_service.materialize_pipeline_lineage(pipeline(policy=POLICY, quality_mode='ENFORCE'), CONN)\n    return await graph()")
    assert ["kafka_topic", PREPARED] in out["nodes"]
    assert ["kafka_topic", QUARANTINE] in out["nodes"]
    assert ["processor", "EMITS_TO", "kafka_topic"] in out["edges"]
    assert ["processor", "QUARANTINES_TO", "kafka_topic"] in out["edges"]


def test_a_policy_that_only_logs_shows_the_processor_but_no_quarantine_topic(db) -> None:
    out = _run(db, "async def main():\n    await lineage_service.materialize_pipeline_lineage(pipeline(policy=LOG_POLICY), CONN)\n    return await graph()")
    assert ["processor", "Log only (quality policy)"] in out["nodes"]
    assert not any(n[1] == QUARANTINE for n in out["nodes"])
    assert not any(e[1] == "QUARANTINES_TO" for e in out["edges"])


def test_removing_the_policy_removes_the_processor_and_its_topics_from_the_graph_and_the_catalog(db) -> None:
    body = """async def main():
    await lineage_service.materialize_pipeline_lineage(pipeline(policy=POLICY, quality_mode='ENFORCE'), CONN)
    before = await graph()
    await lineage_service.materialize_pipeline_lineage(pipeline(), CONN)               # the policy was taken off and the pipeline redeployed
    return {"before": before, "after": await graph(),
            "stale_assets": await count("SELECT count(*) FROM dc_assets WHERE asset_type = 'processor' OR name LIKE '%prepared%' OR name LIKE '%quarantine%'")}"""
    out = _run(db, body)
    assert len(out["before"]["nodes"]) == 6
    assert len(out["after"]["nodes"]) == 3
    assert len(out["after"]["edges"]) == 2
    assert out["stale_assets"] == 0, "no orphaned processor / prepared / quarantine asset is left behind"


def test_switching_from_enforce_back_to_observe_drops_only_the_prepared_topic(db) -> None:
    body = """async def main():
    await lineage_service.materialize_pipeline_lineage(pipeline(policy=POLICY, quality_mode='ENFORCE'), CONN)
    await lineage_service.materialize_pipeline_lineage(pipeline(policy=POLICY, quality_mode='OBSERVE'), CONN)
    return await graph()"""
    out = _run(db, body)
    assert not any(n[1] == PREPARED for n in out["nodes"])
    assert ["kafka_topic", QUARANTINE] in out["nodes"]
    assert ["processor", "Customer checks (quality policy)"] in out["nodes"]


def test_materializing_again_changes_nothing(db) -> None:
    body = """async def main():
    for _ in range(3):
        await lineage_service.materialize_pipeline_lineage(pipeline(policy=POLICY, quality_mode='ENFORCE'), CONN)
    return {"graph": await graph(), "assets": await count("SELECT count(*) FROM dc_assets"), "edges": await count("SELECT count(*) FROM dc_lineage_edges")}"""
    out = _run(db, body)
    assert out["assets"] == 6
    assert out["edges"] == 5


def test_a_quarantine_topic_shared_with_another_pipeline_is_kept_when_only_one_drops_its_policy(db) -> None:
    """The quarantine topic is named from domain / source / entity / version only, so a cdc and a batch pipeline of the same entity share it."""
    body = """async def main():
    a, b = pipeline(11, mode="cdc", policy=POLICY), pipeline(12, mode="batch", policy=POLICY)
    await lineage_service.materialize_pipeline_lineage(a, CONN)
    await lineage_service.materialize_pipeline_lineage(b, CONN)
    await lineage_service.materialize_pipeline_lineage(pipeline(11, mode="cdc"), CONN)      # pa drops its policy
    return {"a": await graph(11), "b": await graph(12)}"""
    out = _run(db, body)
    assert not any(n[0] == "processor" for n in out["a"]["nodes"])
    assert ["kafka_topic", QUARANTINE] in out["b"]["nodes"], "the other pipeline still quarantines to it"
    assert ["processor", "QUARANTINES_TO", "kafka_topic"] in out["b"]["edges"]


def test_one_pipelines_graph_never_contains_another_pipelines_assets(db) -> None:
    body = """async def main():
    await lineage_service.materialize_pipeline_lineage(pipeline(11, entity="orders", policy=POLICY), CONN)
    await lineage_service.materialize_pipeline_lineage(pipeline(12, entity="users"), CONN)
    return {"a": await graph(11), "b": await graph(12)}"""
    out = _run(db, body)
    assert all("users" not in n[1] for n in out["a"]["nodes"])
    assert all("orders" not in n[1] and n[0] != "processor" for n in out["b"]["nodes"])


def test_attaching_or_removing_a_policy_on_a_deployed_pipeline_redraws_its_lineage_without_a_redeploy(db) -> None:
    db.execute("""
        INSERT INTO dc_connector_templates (id, name, connector_type, category, connector_class, runtime_pool_type, config_schema, default_config,
            default_transforms, version, is_active, supported_transforms, supported_kafka_connect_versions, supported_source_versions,
            transform_compatibility, supported_modes, supports_dlq, supports_schema_discovery)
          VALUES ('tpl_jdbc', 'J', 'source', 'database', 'io.confluent.connect.jdbc.JdbcSourceConnector', 'connect-jdbc-pool', '{}', '{}', '[]', '1', true, '[]', '[]', '[]', '{}', '[]', false, false);
    """)
    body = """from app.schemas.pipeline import PipelineCreate, PipelineUpdate
from app.services.pipeline_service import pipeline_service

async def main():
    created = await pipeline_service.create_pipeline(ORG, PipelineCreate(name="hooked", template_id="tpl_jdbc", connection_profile_id="00000000-0000-0000-0000-0000000000a1",
        zone="raw", domain="hk", source_system="s", entity="t", mode="batch", version="v1", connector_config={"tables": ["t"]}))
    pid = created["id"]
    await lineage_service.materialize_pipeline_lineage({**created, "status": "running"}, CONN)             # what a deploy does
    async with AsyncSessionLocal() as s:
        await s.execute(text("UPDATE dc_pipelines SET status = 'running' WHERE id = :i"), {"i": pid}); await s.commit()
    async def shown():
        g = await lineage_service.graph_for_pipeline(ORG, pid)
        return sorted(n["asset_type"] for n in g["nodes"])
    plain = await shown()
    await pipeline_service.update_pipeline(ORG, pid, PipelineUpdate(quality_policy_id=POLICY))
    with_policy = await shown()
    after = await pipeline_service.update_pipeline(ORG, pid, PipelineUpdate(quality_policy_id=None))
    return {"plain": plain, "with_policy": with_policy, "removed": await shown(), "policy_after": after.get("quality_policy_id"), "status_after": after.get("status")}"""
    out = _run(db, body, timeout=180)
    assert out["plain"] == ["connector", "kafka_topic", "source"]
    assert out["with_policy"] == ["connector", "kafka_topic", "kafka_topic", "processor", "source"]      # the policy and its quarantine topic appear
    assert out["policy_after"] is None, "removing the policy through an edit really removes it"
    assert out["removed"] == ["connector", "kafka_topic", "source"], out                                      # and go again
