"""WS-FE-014: the dbt / Flink / DQ proxy routes publish typed response contracts.

The proxies forward upstream bytes untouched (they return a ``Response``), so
these models are documentation for the OpenAPI document the SPA generates its
types from. These tests pin that every proxied JSON route declares one, that
the models tolerate upstream additions, and that /auth/me is typed.
"""
from __future__ import annotations

import re

import pytest

from app.main import app
from app.schemas import proxy

_PROXY_PREFIX = re.compile(r"^/api/v1/(dbt|flink|dq)/")


def _operations():
    for path, ops in app.openapi()["paths"].items():
        for method, op in ops.items():
            yield path, method, op


def test_every_proxy_route_declares_a_response_schema():
    missing = []
    for path, method, op in _operations():
        if not _PROXY_PREFIX.match(path):
            continue
        ok = {code: r for code, r in op["responses"].items() if code.startswith("2")}
        assert ok, f"{method.upper()} {path} documents no 2xx response"
        for code, resp in ok.items():
            if code == "204":
                continue
            schema = resp.get("content", {}).get("application/json", {}).get("schema", {})
            if "$ref" not in schema and "$ref" not in schema.get("items", {}):
                missing.append(f"{method.upper()} {path} ({code})")
    assert not missing, f"proxy routes without a response model: {missing}"


@pytest.mark.parametrize("path,method", [
    ("/api/v1/dbt/projects/register", "post"),
    ("/api/v1/dbt/projects/{project_id}", "patch"),
    ("/api/v1/flink/jobs", "post"),
    ("/api/v1/dq/bindings", "post"),
])
def test_pass_through_request_bodies_are_documented(path, method):
    body = app.openapi()["paths"][path][method]["requestBody"]
    assert body["content"]["application/json"]["schema"]["properties"]


def test_proxy_models_tolerate_upstream_additions():
    job = proxy.FlinkJob.model_validate({
        "id": "00000000-0000-0000-0000-000000000001", "org_id": "o", "workspace_id": None,
        "name": "j", "description": None, "sql": "select 1", "parallelism": 1,
        "sink_engine": "trino", "status": "running", "validation_errors": [],
        "last_deployed_at": None, "last_stopped_at": None, "savepoint_path": None,
        "created_at": "2026-01-01T00:00:00Z", "brand_new_upstream_field": 42,
    })
    assert job.model_dump()["brand_new_upstream_field"] == 42
    schema = app.openapi()["components"]["schemas"]["FlinkJob"]
    assert schema.get("additionalProperties") is True


def test_auth_me_is_typed():
    op = app.openapi()["paths"]["/api/v1/auth/me"]["get"]
    ref = op["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    assert ref.endswith("/MeOut")


def test_dbt_register_documents_project_path():
    """Repos can hold several dbt projects; the folder travels as project_path."""
    props = app.openapi()["paths"]["/api/v1/dbt/projects/register"]["post"]["requestBody"][
        "content"]["application/json"]["schema"]["properties"]
    assert "project_path" in props
    assert proxy.DbtProjectRegister.model_validate(
        {"git_url": "https://h/o/r.git", "project_path": "sales"}).project_path == "sales"
    assert "project_path" in app.openapi()["components"]["schemas"]["DbtProject"]["properties"]


# ── DBT-005: the run statuses are a contract with the dbt runner ─────────
def test_the_dbt_run_status_values_match_the_dbt_runner():
    """The runner reports `cancelling` while a run's Kubernetes Job is being terminated (DBT-005); Workspace's
    schema, the committed OpenAPI file and the SPA's generated types must know every status the runner can send.
    Reads the runner repo when it is checked out next to this one (or at RUNNERS_REPO); skipped otherwise."""
    import os
    import re
    from pathlib import Path

    import pytest
    from typing import get_args

    from app.schemas.proxy import DbtRunStatus

    runner = Path(os.environ.get("RUNNERS_REPO", Path(__file__).resolve().parents[2] / "Pralai_workspace_runners")) \
        / "pralai-dbt-runner" / "app" / "schemas" / "project.py"
    if not runner.exists():
        pytest.skip("runner repo not checked out next to this one")
    declared = re.search(r"^RunStatus = Literal\[(.*?)\]", runner.read_text(), re.M | re.S).group(1)
    assert set(re.findall(r'"([a-z]+)"', declared)) == set(get_args(DbtRunStatus))


def test_the_committed_openapi_lists_cancelling_as_a_dbt_run_status():
    import json
    from pathlib import Path

    spec = json.loads((Path(__file__).resolve().parent.parent / "openapi" / "workspace.json").read_text())
    assert "cancelling" in spec["components"]["schemas"]["DbtRun"]["properties"]["status"]["enum"]


# ── DG-008 (and the credential_ref added in DBT-009): the documented project shapes cover what the dbt runner accepts and returns ──
def test_the_documented_dbt_project_shapes_cover_the_runners_fields():
    """The proxy forwards bodies untouched, so nothing breaks at runtime when the runner gains a field — it just goes UNDOCUMENTED in the
    OpenAPI document and the SPA's types (credential_ref was missing for exactly that reason). Compare field names with the runner."""
    import ast
    import os
    from pathlib import Path

    import pytest

    runner = Path(os.environ.get("RUNNERS_REPO", Path(__file__).resolve().parents[2] / "Pralai_workspace_runners")) \
        / "pralai-dbt-runner" / "app" / "schemas" / "project.py"
    if not runner.exists():
        pytest.skip("runner repo not checked out next to this one")
    tree = ast.parse(runner.read_text())
    runner_fields = {c.name: {s.target.id for s in c.body if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name)}
                     for c in tree.body if isinstance(c, ast.ClassDef)}
    # runner field -> accepted-only-from-a-trusted-caller / not part of the browser-facing shape
    not_proxied = {"ProjectRegister": set(), "ProjectUpdate": set(), "ProjectOut": {"org_id_unused"}}
    for runner_model, proxy_model in (("ProjectRegister", proxy.DbtProjectRegister), ("ProjectUpdate", proxy.DbtProjectUpdate),
                                      ("ProjectOut", proxy.DbtProject)):
        missing = runner_fields[runner_model] - set(proxy_model.model_fields) - not_proxied[runner_model]
        assert not missing, f"{runner_model}: the runner has {sorted(missing)} but the Workspace's documented {proxy_model.__name__} does not"


def test_dbt_project_documents_dependents_and_accepts_an_older_runner_without_them():
    """Dependents: the register/update bodies and the project carry them; a runner that predates them still validates."""
    base = {
        "id": "00000000-0000-0000-0000-000000000001", "org_id": "o", "workspace_id": None, "name": "combined",
        "git_url": "https://github.com/acme/x.git", "git_ref": "main", "engine": "pg_lake", "schedule_cron": None,
        "schedule_enabled": False, "last_synced_sha": None, "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z", "last_run": None,
    }
    old = proxy.DbtProject.model_validate(base)
    assert old.dependent_project_ids == [] and old.upstream_project_ids == []
    new = proxy.DbtProject.model_validate({**base, "dependent_project_ids": ["00000000-0000-0000-0000-000000000002"]})
    assert str(new.dependent_project_ids[0]).endswith("2")
    assert "blocked_by" not in proxy.DbtProject.model_fields            # the dependency rule is enforced in Dagster, not reported here
    for path, method in (("/api/v1/dbt/projects/register", "post"), ("/api/v1/dbt/projects/{project_id}", "patch")):
        props = app.openapi()["paths"][path][method]["requestBody"]["content"]["application/json"]["schema"]["properties"]
        assert "dependent_project_ids" in props




# ── ClickHouse as a job engine: Workspace, the committed OpenAPI file and the dbt runner must agree on the list ──
def test_a_dbt_sql_or_python_job_can_be_registered_on_clickhouse():
    for kind in ("dbt", "sql", "python"):
        body = proxy.DbtProjectRegister.model_validate({"git_url": "https://h/o/r.git", "kind": kind, "engine": "clickhouse"})
        assert body.engine == "clickhouse" and body.model_dump()["engine"] == "clickhouse"
    assert proxy.DbtProjectRegister.model_validate({"git_url": "https://h/o/r.git"}).engine == "trino"       # the default is unchanged
    import pytest
    with pytest.raises(Exception):
        proxy.DbtProjectRegister.model_validate({"git_url": "https://h/o/r.git", "engine": "oracle"})


def test_the_job_engines_match_the_dbt_runner_and_the_committed_openapi():
    import json
    import os
    import re
    from pathlib import Path
    from typing import get_args

    import pytest

    spec = json.loads((Path(__file__).resolve().parent.parent / "openapi" / "workspace.json").read_text())
    assert set(spec["components"]["schemas"]["DbtProject"]["properties"]["engine"]["enum"]) == set(get_args(proxy.DbtEngine))
    register = spec["paths"]["/api/v1/dbt/projects/register"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert set(register["properties"]["engine"]["enum"]) == set(get_args(proxy.DbtEngine))
    runner = Path(os.environ.get("RUNNERS_REPO", Path(__file__).resolve().parents[2] / "Pralai_workspace_runners")) \
        / "pralai-dbt-runner" / "app" / "schemas" / "project.py"
    if not runner.exists():
        pytest.skip("runner repo not checked out next to this one")
    declared = re.search(r"^Engine = Literal\[(.*?)\]", runner.read_text(), re.M | re.S).group(1)
    assert set(re.findall(r'"([a-z_]+)"', declared)) == set(get_args(proxy.DbtEngine))
