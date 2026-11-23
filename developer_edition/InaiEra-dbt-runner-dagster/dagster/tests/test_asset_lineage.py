"""Dagster's Lineage page shows the dbt projects and their DATA dependencies.

Each code location defined jobs and sensors only, so Lineage was empty. Now every project is an asset keyed
``<code location>/<job name>``; its dependencies are the projects whose data it reads (`data_upstream_project_ids`, worked out by the
dbt Runner from dbt's lineage), across repositories too. A cycle would stop the whole location from loading, so cycles are broken the
same way everywhere. A successful dbt run that Dagster submitted records a materialization of the project's asset.

Real Dagster (definitions, an in-memory instance); only the Runner's HTTP side is faked.
"""
from __future__ import annotations

from dagster import AssetKey, DagsterEventType, DagsterInstance, EventRecordsFilter

from inaiera_dbt_runner_dags import monitoring
from inaiera_dbt_runner_dags.assets import (
    asset_group,
    data_upstream,
    project_asset_specs,
)
from inaiera_dbt_runner_dags.jobs.dbt_run_job import (
    dbt_run_result_job,
    make_project_job,
)
from inaiera_dbt_runner_dags.location_defs import build_location_defs
from inaiera_dbt_runner_dags.naming import location_fingerprint, location_name

REPO = "https://github.com/lithishbabu/inaiera-dbt-test.git"
OTHER = "https://github.com/acme/finance.git"


def _p(pid, folder, ups=(), git_url=REPO):
    return {"id": pid, "name": f"{git_url.rsplit('/', 1)[-1][:-4]}/{folder}", "git_url": git_url, "git_ref": "main",
            "project_path": folder, "schedule_cron": None, "updated_at": "2026-01-01T00:00:00+00:00",
            "data_upstream_project_ids": list(ups)}


PROJECTS = [_p("c", "customers"), _p("p", "products", ["c"]), _p("r", "revenue", ["c"]), _p("x", "combined", ["p", "r"])]
LOC = location_name(PROJECTS[0])


def _key(loc, job):
    return AssetKey([loc, job])


def _graph(defs):
    g = defs.resolve_asset_graph()
    return {k: set(g.get(k).parent_keys) for k in g.get_all_asset_keys() if g.has(k) and g.get(k).is_materializable is False}


def test_every_project_is_an_asset_linked_by_the_data_it_reads():
    specs = {s.key: s for s in project_asset_specs(LOC, PROJECTS)}
    assert set(specs) == {_key(LOC, j) for j in ("customers", "products", "revenue", "combined")}
    assert {d.asset_key for d in specs[_key(LOC, "combined")].deps} == {_key(LOC, "products"), _key(LOC, "revenue")}
    assert {d.asset_key for d in specs[_key(LOC, "products")].deps} == {_key(LOC, "customers")}
    assert not specs[_key(LOC, "customers")].deps
    assert all(s.group_name == asset_group(LOC) and "-" not in s.group_name for s in specs.values())
    assert specs[_key(LOC, "products")].metadata["project_id"] == "p"


def test_the_code_location_loads_with_its_assets_and_their_lineage():
    defs = build_location_defs(LOC, PROJECTS)
    graph = defs.resolve_asset_graph()
    assert graph.get(_key(LOC, "combined")).parent_keys == {_key(LOC, "products"), _key(LOC, "revenue")}
    assert {j.name for j in defs.resolve_all_job_defs()} >= {"customers", "products", "revenue", "combined", "dbt_run_result"}


def test_a_dependency_on_a_project_in_another_repository_points_at_its_key():
    finance = _p("f", "ledger", ["c"], git_url=OTHER)
    specs = {s.key: s for s in project_asset_specs(location_name(finance), [*PROJECTS, finance])}
    (spec,) = specs.values()
    assert {d.asset_key for d in spec.deps} == {_key(LOC, "customers")}


def test_a_cycle_is_broken_identically_in_every_location_and_the_location_still_loads():
    a, b = _p("a", "alpha", ["b"]), _p("b", "beta", ["a"], git_url=OTHER)
    kept = data_upstream([a, b])
    assert kept == {"b": ["a"]}                  # visited by name: finance/beta keeps its edge, inaiera-dbt-test/alpha's closing one goes
    assert data_upstream([b, a]) == kept                           # same answer whatever the listing order
    for project in (a, b):
        build_location_defs(location_name(project), [a, b]).resolve_asset_graph()   # no CircularDependencyError


def test_unknown_and_self_upstreams_are_ignored():
    assert data_upstream([_p("a", "alpha", ["a", "gone"])]) == {}


def test_a_change_in_data_dependencies_reloads_the_location():
    before = location_fingerprint(PROJECTS)
    changed = [*PROJECTS[:3], _p("x", "combined", ["p"])]
    assert location_fingerprint(changed) != before
    assert location_fingerprint(list(reversed(PROJECTS))) == before


# ── a successful dbt run records a materialization of the project's asset ─────────────────────────────────────────────
class _Runner:
    base_url = "http://runner.test"

    def __init__(self, status):
        self.status = status

    def get_project(self, project_id):                    # the start check (dependencies.py): no links, so nothing above it
        return {"id": project_id, "upstream_project_ids": []}

    def list_projects(self):
        return []

    def trigger_run(self, project_id, **kw):
        return {"id": "run-1", "status": "queued", "git_sha": kw.get("git_sha")}

    def get_run(self, run_id):
        return {"id": run_id, "status": self.status, "git_sha": "a" * 40, "models_total": 55, "duration_ms": 4200}


def _materializations(instance):
    return instance.get_event_records(EventRecordsFilter(event_type=DagsterEventType.ASSET_MATERIALIZATION))


def _submit_and_report(status, location=LOC):
    instance = DagsterInstance.ephemeral()
    runner = _Runner(status)
    sub = make_project_job("customers", "c", "inaiera-dbt-test/customers", location).execute_in_process(
        instance=instance, resources={"dbt_runner": runner}).run_id
    dbt_run_result_job.execute_in_process(instance=instance, resources={"dbt_runner": runner}, raise_on_error=False,
                                          run_config={"ops": {"report_run_result_op": {"config": {"run_id": "run-1", "submitted_by": sub}}}})
    return _materializations(instance)


def _by_key(records):
    return {r.event_log_entry.dagster_event.event_specific_data.materialization.asset_key:
            r.event_log_entry.dagster_event.event_specific_data.materialization for r in records}


def test_a_successful_run_materializes_the_projects_asset():
    mats = _by_key(_submit_and_report("succeeded"))
    mat = mats[_key(LOC, "customers")]
    assert mat.metadata["dbt_run_id"].value == "run-1" and mat.metadata["models_total"].value == 55
    assert set(mats) == {_key(LOC, "customers"), AssetKey(["job_lineage", LOC, "customers"])}      # the data node and the job node


def test_a_failed_or_cancelled_run_materializes_nothing():
    assert _submit_and_report("failed") == [] and _submit_and_report("cancelled") == []


def test_the_generic_job_has_no_project_asset_to_materialize():
    assert _submit_and_report("succeeded", location=monitoring.GENERIC_LOCATION) == []


# ── SQL jobs (kind "sql"): a job in Dagster, not an asset ─────────────────────────────────────────────────────────────────
def _sql_job(pid="s", path="jobs/daily_totals.sql"):
    return {**_p(pid, path), "kind": "sql"}


def test_a_sql_job_is_a_job_named_after_its_file_and_a_data_lineage_box():
    projects = [*PROJECTS, _sql_job()]
    defs = build_location_defs(LOC, projects)
    jobs = {j.name: j for j in defs.resolve_all_job_defs()}
    assert "daily_totals" in jobs and jobs["daily_totals"].tags["project_kind"] == "sql"
    assert "SQL job" in jobs["daily_totals"].description
    assert jobs["customers"].tags["project_kind"] == "dbt"
    assert _key(LOC, "daily_totals") in {s.key for s in project_asset_specs(LOC, projects)}      # a SQL job reports its reads and writes


def test_a_successful_sql_job_run_records_its_data_node_and_its_job_node():
    instance = DagsterInstance.ephemeral()
    runner = _Runner("succeeded")
    sub = make_project_job("daily_totals", "s", "jobs/daily_totals", LOC, kind="sql").execute_in_process(
        instance=instance, resources={"dbt_runner": runner}).run_id
    dbt_run_result_job.execute_in_process(instance=instance, resources={"dbt_runner": runner}, raise_on_error=False,
                                          run_config={"ops": {"report_run_result_op": {"config": {"run_id": "run-1", "submitted_by": sub}}}})
    assert set(_by_key(_materializations(instance))) == {_key(LOC, "daily_totals"), AssetKey(["job_lineage", LOC, "daily_totals"])}


def _python_job(pid="y", path="jobs/pipeline.py"):
    return {**_p(pid, path), "kind": "python"}


def test_a_python_job_is_a_job_named_after_its_script_and_not_a_lineage_box():
    projects = [*PROJECTS, _python_job()]
    jobs = {j.name: j for j in build_location_defs(LOC, projects).resolve_all_job_defs()}
    assert "pipeline" in jobs and jobs["pipeline"].tags["project_kind"] == "python" and "Python job" in jobs["pipeline"].description
    assert _key(LOC, "pipeline") not in {s.key for s in project_asset_specs(LOC, projects)}


def test_a_successful_python_job_run_records_its_job_node_only():
    instance = DagsterInstance.ephemeral()
    runner = _Runner("succeeded")
    sub = make_project_job("pipeline", "y", "jobs/pipeline", LOC, kind="python").execute_in_process(
        instance=instance, resources={"dbt_runner": runner}).run_id
    dbt_run_result_job.execute_in_process(instance=instance, resources={"dbt_runner": runner}, raise_on_error=False,
                                          run_config={"ops": {"report_run_result_op": {"config": {"run_id": "run-1", "submitted_by": sub}}}})
    assert set(_by_key(_materializations(instance))) == {AssetKey(["job_lineage", LOC, "pipeline"])}
