"""Migrations: from scratch (session fixture) AND 0002 -> head on a database
seeded like the live one (rules incl. custom_sql, rulesets, bindings,
duplicate-hash legacy artifact versions). Runs in its own throwaway
*_test database created next to TEST_DATABASE_URL."""
import uuid

import pytest
from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from tests.conftest import alembic_config, check_test_database_url
from app.db.migration_url import sync_migration_url


@pytest.fixture
def mig_url(test_database_url):
    base = make_url(test_database_url)
    url = base.set(database="dqmig_test").render_as_string(hide_password=False)
    check_test_database_url(url)
    admin = create_engine(base.set(drivername="postgresql+psycopg2").render_as_string(hide_password=False),
                          isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text("DROP DATABASE IF EXISTS dqmig_test"))
        c.execute(text("CREATE DATABASE dqmig_test"))
    yield url
    with admin.connect() as c:
        c.execute(text("DROP DATABASE IF EXISTS dqmig_test WITH (FORCE)"))
    admin.dispose()


def _sync(url):
    return create_engine(sync_migration_url(url))


def test_upgrade_from_0002_preserves_live_data(mig_url):
    cfg = alembic_config(mig_url)
    command.upgrade(cfg, "0002_artifact_versions")
    eng = _sync(mig_url)
    rule_ids = [uuid.uuid4() for _ in range(3)]
    rs_id, b_id = uuid.uuid4(), uuid.uuid4()
    with eng.begin() as c:
        for rid, (name, rt, params) in zip(rule_ids, [
                ("nn", "not_null", "{}"), ("csql", "custom_sql", '{"condition": "a > 0"}'),
                ("rng", "range", '{"min": 0}')]):
            c.execute(text(
                "INSERT INTO dq_rules (id, org_id, name, rule_type, dimension, parameters, tags, "
                "rendered_dbt_yaml) VALUES (:id, 'org', :n, :rt, 'validity', CAST(:p AS jsonb), "
                "'{a,b}', 'cached')"), {"id": rid, "n": name, "rt": rt, "p": params})
        c.execute(text("INSERT INTO dq_rulesets (id, org_id, name) VALUES (:id, 'org', 'rs')"),
                  {"id": rs_id})
        for i, rid in enumerate(rule_ids):
            c.execute(text("INSERT INTO dq_ruleset_items (ruleset_id, rule_id, column_name, "
                           "sort_order) VALUES (:rs, :r, :c, :o)"),
                      {"rs": rs_id, "r": rid, "c": f"col{i}", "o": i})
        c.execute(text(
            "INSERT INTO dq_job_bindings (id, org_id, ruleset_id, job_type, job_id, target_table, "
            "ucs_fqn, enforcement_mode) VALUES (:id, 'org', :rs, 'dbt_project', :j, 'orders', "
            "'pg.public.orders', 'blocking')"), {"id": b_id, "rs": rs_id, "j": uuid.uuid4()})
        for _ in range(2):   # duplicate hashes, as old forced re-renders produced
            c.execute(text(
                "INSERT INTO dq_artifact_versions (binding_id, content_hash, renderer_version, "
                "engine_version, dbt_artifact) VALUES (:b, 'h1', '1.1.0', 'dbt-schema-v2', 'x')"),
                {"b": b_id})

    command.upgrade(cfg, "head")

    with eng.begin() as c:
        rules = {r.name: r for r in c.execute(text(
            "SELECT name, version, privileged, review_status, tags, parameters FROM dq_rules"))}
        assert len(rules) == 3 and rules["nn"].version == 1 and rules["nn"].tags == ["a", "b"]
        assert rules["csql"].privileged is True and rules["csql"].review_status == "pending"
        assert rules["rng"].parameters == {"min": 0} and rules["nn"].review_status is None
        cols = {r[0] for r in c.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name='dq_rules'"))}
        assert not any(x.startswith("rendered_") for x in cols)
        assert c.execute(text("SELECT count(*) FROM dq_ruleset_items")).scalar() == 3
        b = c.execute(text("SELECT version, resolved_at, flink_fail_stop, enforcement_mode "
                           "FROM dq_job_bindings")).one()
        assert b.version == 1 and b.resolved_at is None and b.flink_fail_stop is False
        assert b.enforcement_mode == "blocking"
        kinds = [r[0] for r in c.execute(text("SELECT kind FROM dq_artifact_versions"))]
        assert kinds == ["legacy", "legacy"]
        assert c.execute(text("SELECT version FROM dq_rulesets")).scalar() == 1

    # artifact rows are immutable from here on
    with pytest.raises(Exception, match="immutable"):
        with eng.begin() as c:
            c.execute(text("UPDATE dq_artifact_versions SET dbt_artifact='tampered'"))
    # but cascade delete with the binding still works
    with eng.begin() as c:
        c.execute(text("DELETE FROM dq_job_bindings"))
        assert c.execute(text("SELECT count(*) FROM dq_artifact_versions")).scalar() == 0

    # round-trip: downgrade then upgrade again
    command.downgrade(cfg, "0002_artifact_versions")
    command.upgrade(cfg, "head")
    eng.dispose()


def test_head_schema_matches_models(migrated_db):
    """Every mapped table/column exists in the migrated schema."""
    from app.db.base import Base
    import app.models  # noqa: F401
    eng = _sync(migrated_db)
    with eng.connect() as c:
        for table in Base.metadata.sorted_tables:
            cols = {r[0] for r in c.execute(text(
                "SELECT column_name FROM information_schema.columns WHERE table_name=:t"),
                {"t": table.name})}
            assert cols, table.name
            missing = {col.name for col in table.columns} - cols
            assert not missing, (table.name, missing)
    eng.dispose()
