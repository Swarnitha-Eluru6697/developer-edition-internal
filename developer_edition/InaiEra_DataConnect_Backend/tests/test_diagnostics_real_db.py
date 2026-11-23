"""
Hardening, against a REAL PostgreSQL: the diagnostics bundle is tenant-scoped, never carries a planted secret, and the CLI emits clean JSON.

Uses a scratch database (tests/scratch_db.py) built the way a fresh deployment is (models + stamped at head). Skipped unless
DATACONNECT_REAL_DB_TESTS=1.
"""

import json
import os
import subprocess
import sys

import pytest
from scratch_db import BACKEND, scratch_database

pytestmark = pytest.mark.skipif(not os.environ.get("DATACONNECT_REAL_DB_TESTS"), reason="needs a real PostgreSQL (DATACONNECT_REAL_DB_TESTS=1)")

SECRET = "S3CR3T-MARKER-7be1c0d2a94f"


def _seed(db) -> None:
    for org, tag in (("org_a", "a"), ("org_b", "b")):
        db.execute(f"""
            INSERT INTO dc_organizations (org_id, name, status) VALUES ('{org}', 'Org {tag.upper()} CONFIDENTIAL NAME', 'active');
            INSERT INTO dc_connector_templates (id, name, connector_type, category, connector_class, runtime_pool_type, config_schema, default_config,
                default_transforms, version, is_active, supported_transforms, supported_kafka_connect_versions, supported_source_versions,
                transform_compatibility, supported_modes, supports_dlq, supports_schema_discovery)
              VALUES ('tpl_{tag}', 'T', 'source', 'database', 'x.Y', 'connect-jdbc-pool', '{{}}', '{{}}', '[]', '1', true, '[]', '[]', '[]', '{{}}', '[]', false, false);
            INSERT INTO dc_connection_profiles (id, org_id, name, source_type, connection_config, secret_ref)
              VALUES ('00000000-0000-0000-0000-0000000000{tag}1', '{org}', 'conn-{tag}-PRIVATE', 'postgresql',
                      '{{"hostname": "h-{tag}", "password": "{SECRET}"}}', 'secret://dataconnect/connections/x');
            INSERT INTO dc_pipelines (id, org_id, name, template_id, connection_profile_id, zone, domain, source_system, entity, mode, version, direction,
                connector_name, connector_config, generated_config, template_snapshot, status, error_handling_policy, runtime_pool_id, topic_name)
              VALUES ('00000000-0000-0000-0000-0000000000{tag}2', '{org}', 'pipe-{tag}-PRIVATE', 'tpl_{tag}', '00000000-0000-0000-0000-0000000000{tag}1',
                      'raw', 'd', 's', 'e', 'batch', 'v1', 'SOURCE', 'c-{tag}', '{{"tables": ["t"], "password": "{SECRET}"}}', '{{}}', '{{}}', 'running',
                      'FAIL_FAST', 'connect-jdbc-pool', 'raw-d-s-e-batch-v1');
            INSERT INTO dc_operations (id, org_id, operation_type, resource_type, resource_id, status, error_code, error_message, retryable, correlation_id, completed_at)
              VALUES ('00000000-0000-0000-0000-0000000000{tag}3', '{org}', 'pipeline_deploy', 'pipeline', 'p', 'failed', 'KAFKA_CONNECT_UNAVAILABLE',
                      'driver said postgresql://u:{SECRET}@h/db password={SECRET}', true, 'corr-{tag}-0001', now());
        """)


def _bundle(db, org: str | None) -> dict:
    code = ("import asyncio, json, sys\nfrom app.services.diagnostics import build_diagnostics_bundle\n"
            f"b = asyncio.run(build_diagnostics_bundle({org!r}))\nopen(sys.argv[1], 'w').write(json.dumps(b, default=str))\n")
    out = BACKEND / f".diag-{db.name}.json"
    try:
        result = subprocess.run([sys.executable, "-c", code, str(out)], cwd=BACKEND, env=db.env(), capture_output=True, text=True, timeout=120)
        assert result.returncode == 0, result.stderr[-1500:]
        return json.loads(out.read_text())
    finally:
        out.unlink(missing_ok=True)


@pytest.fixture
def seeded():
    with scratch_database("dc_diag") as db:
        assert db.startup_migrations().returncode == 0
        _seed(db)
        yield db


def test_an_organizations_bundle_contains_only_its_own_counts_and_failures(seeded) -> None:
    bundle = _bundle(seeded, "org_a")
    text = json.dumps(bundle)

    assert bundle["failed_sections"] == []
    assert bundle["database"]["counts"]["connections"] == 1
    assert bundle["database"]["counts"]["pipelines_by_status"] == {"running": 1}
    assert [o["id"] for o in bundle["recent_failures"]["operations"]] == ["00000000-0000-0000-0000-0000000000a3"]
    assert bundle["recent_failures"]["operations"][0]["correlation_id"] == "corr-a-0001"
    for other_tenant in ("org_b", "corr-b-0001", "0000000000b3", "conn-b", "pipe-b", "Org B"):
        assert other_tenant not in text, f"{other_tenant!r} leaked into org_a's bundle"


def test_the_bundle_never_carries_secrets_names_or_free_text_messages(seeded) -> None:
    text = json.dumps(_bundle(seeded, "org_a"))

    assert SECRET not in text                               # not in a connection config, a connector config or an error message
    for private in ("PRIVATE", "CONFIDENTIAL", "h-a", "driver said", "secret://"):
        assert private not in text, f"{private!r} reached the bundle"


def test_the_migration_state_is_reported_and_current(seeded) -> None:
    migration = _bundle(seeded, "org_a")["database"]["migration"]

    assert migration["up_to_date"] is True
    assert migration["current"] == migration["head"]


def test_the_cli_aggregates_every_organization_and_writes_only_json_to_stdout(seeded) -> None:
    result = subprocess.run([sys.executable, "-m", "app.diagnostics"], cwd=BACKEND, env=seeded.env(), capture_output=True, text=True, timeout=120)

    assert result.returncode == 0, result.stderr[-1500:]
    bundle = json.loads(result.stdout)                      # a single JSON document: logging went to stderr
    assert bundle["service"]["scope"] == {"deployment": "all organizations (aggregated)"}
    assert bundle["database"]["counts"]["connections"] == 2
    assert "note" in bundle["workers"]
    assert SECRET not in result.stdout
    assert SECRET not in result.stderr
