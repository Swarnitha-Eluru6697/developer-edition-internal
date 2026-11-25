"""Regression tests for RUN-DBT-001: DbtExecutor.run_project() must run a
real `dbt build` end to end (the original invocation passed a literal
Ellipsis and its cleanup referenced an undefined name), and — since the
run_id-gated lineage build path was never exercised before — must also
build the OpenLineage event from the real dbt artifacts.

Runs a minimal real dbt project against the guarded throwaway *_test
Postgres from TEST_DATABASE_URL (see conftest.py) acting as the "pg_lake"
engine. Skipped when TEST_DATABASE_URL is unset; refuses to run against a
database that is not named *_test. Never targets any other database.
"""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url

from app.core.config import Settings
from app.executors.dbt_executor import DbtExecutor


@pytest.fixture
def pg_lake_dsn(migrated_test_database) -> str:
    """libpq-style DSN for dbt-postgres, derived from the guarded test URL."""
    url = make_url(migrated_test_database).set(drivername="postgresql")
    assert (url.database or "").endswith("_test")
    return url.render_as_string(hide_password=False)


def _write_fixture_project(project_dir: Path) -> None:
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "dbt_project.yml").write_text(
        "name: 'dbtrunner_fixture'\n"
        "version: '1.0.0'\n"
        "profile: 'dbtrunner_fixture'\n"
        "model-paths: ['models']\n"
    )
    models_dir = project_dir / "models"
    models_dir.mkdir()
    (models_dir / "fixture_one.sql").write_text("select 1 as id")
    (models_dir / "fixture_two.sql").write_text("select id from {{ ref('fixture_one') }}")
    (models_dir / "schema.yml").write_text(
        "version: 2\n"
        "models:\n"
        "  - name: fixture_two\n"
        "    columns:\n"
        "      - name: id\n"
        "        tests:\n"
        "          - not_null\n"
        "          - relationships:\n"
        "              to: ref('fixture_one')\n"
        "              field: id\n"
    )


def test_run_project_executes_real_dbt_build_and_builds_lineage(tmp_path, monkeypatch, pg_lake_dsn):
    monkeypatch.setenv("DO_NOT_TRACK", "1")
    monkeypatch.setattr(
        "app.executors.dbt_executor.get_settings",
        lambda: Settings(pg_lake_dsn=pg_lake_dsn, openlineage_namespace="pg_test"),
    )

    project_dir = tmp_path / "project"
    _write_fixture_project(project_dir)
    run_id = uuid.uuid4()

    result = DbtExecutor().run_project(project_dir=project_dir, engine="pg_lake", select=None,
                                       run_id=run_id)

    assert result["success"] is True, result["errors"]
    assert result["nodes"], "expected at least one executed node"
    assert all(n["status"] in ("success", "pass") for n in result["nodes"])
    assert result["manifest"] is not None

    # The lineage build path (only taken when run_id is passed).
    event = result["lineage_event"]
    assert event is not None
    assert event["run"]["runId"] == str(run_id)
    outputs = {o["name"]: o for o in event["outputs"]}
    assert set(outputs) == {"public.fixture_one", "public.fixture_two"}
    assert {"namespace": "pg_test", "name": "public.fixture_one"} in event["inputs"]
    # RUN-DBT-006: both tests attach to the TESTED model (fixture_two) —
    # the relationships test must not land on its `to` model.
    two = outputs["public.fixture_two"]["facets"]["dataQualityAssertions"]["assertions"]
    assert len(two) == 2 and all(a["success"] for a in two)
    assert "dataQualityAssertions" not in outputs["public.fixture_one"]["facets"]


def test_run_project_reports_dbt_failures_without_crashing_cleanup(tmp_path, monkeypatch, pg_lake_dsn):
    monkeypatch.setenv("DO_NOT_TRACK", "1")
    monkeypatch.setattr(
        "app.executors.dbt_executor.get_settings",
        lambda: Settings(pg_lake_dsn=pg_lake_dsn, openlineage_url="http://example.invalid/lineage"),
    )

    project_dir = tmp_path / "project"
    _write_fixture_project(project_dir)
    (project_dir / "models" / "broken.sql").write_text("select * from this_table_does_not_exist")

    result = DbtExecutor().run_project(project_dir=project_dir, engine="pg_lake", select=None,
                                       run_id=uuid.uuid4())

    assert result["success"] is False
    assert result["errors"]
