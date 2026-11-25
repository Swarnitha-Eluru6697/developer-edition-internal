"""Dagster's Lineage page shows the JOB dependencies next to the DATA dependencies.

Every job, of any kind (dbt, SQL, Python), is a node in the group ``job_lineage``, keyed ``job_lineage/<code location>/<job name>``; an arrow
runs from each Upstream Job (`upstream_project_ids`, the job links set in Workspace) to its downstream job, across repositories too. The data
lineage (dbt projects, `data_upstream_project_ids`) is unchanged; the two sets are separate nodes. A location reloads when a job link changes.

Real Dagster (definitions); only the Runner's data is faked.
"""
from __future__ import annotations

from dagster import AssetKey

from pralai_dbt_runner_dags.assets import (
    JOB_LINEAGE_GROUP,
    job_asset_key,
    job_lineage_asset_specs,
    job_upstream,
    project_asset_specs,
)
from pralai_dbt_runner_dags.location_defs import build_location_defs
from pralai_dbt_runner_dags.naming import location_fingerprint, location_name

REPO = "https://github.com/lithishbabu/inaiera-dbt-test.git"
OTHER = "https://github.com/acme/finance.git"


def _p(pid, path, ups=(), kind="dbt", git_url=REPO, data=(), cron=None, engine="trino"):
    return {"id": pid, "name": f"{git_url.rsplit('/', 1)[-1][:-4]}/{path}", "git_url": git_url, "git_ref": "main", "project_path": path,
            "kind": kind, "engine": engine, "schedule_cron": cron, "schedule_enabled": cron is not None,
            "updated_at": "2026-01-01T00:00:00+00:00", "upstream_project_ids": list(ups), "data_upstream_project_ids": list(data)}


# customers (dbt) -> daily (sql) -> report (python) -> ledger (dbt, another repo); products (dbt) also feeds report
PROJECTS = [
    _p("c", "customers"),
    _p("d", "jobs/daily.sql", ["c"], kind="sql", cron="*/5 * * * *"),
    _p("p", "products"),
    _p("r", "jobs/report.py", ["d", "p"], kind="python"),
]
FINANCE = _p("f", "ledger", ["r"], git_url=OTHER)
ALL = [*PROJECTS, FINANCE]
LOC = location_name(PROJECTS[0])
FIN = location_name(FINANCE)


def _key(loc, job):
    return job_asset_key(loc, job)


def _specs(location=LOC, projects=ALL):
    return {s.key: s for s in job_lineage_asset_specs(location, projects)}


def _deps(spec):
    return {d.asset_key for d in spec.deps}


def test_every_job_of_every_kind_is_a_node_in_the_job_lineage_group():
    specs = _specs()
    assert set(specs) == {_key(LOC, j) for j in ("customers", "daily", "products", "report")}
    assert JOB_LINEAGE_GROUP == "job_lineage" and {s.group_name for s in specs.values()} == {"job_lineage"}
    assert {s.metadata["kind"] for s in specs.values()} == {"dbt", "sql", "python"}


def test_the_key_is_the_group_the_code_location_and_the_job_name():
    assert job_asset_key("loc", "customers") == AssetKey(["job_lineage", "loc", "customers"])
    assert _key(LOC, "daily").path == ["job_lineage", LOC, "daily"]


def test_arrows_are_the_job_links_from_upstream_to_downstream():
    specs = _specs()
    assert _deps(specs[_key(LOC, "daily")]) == {_key(LOC, "customers")}
    assert _deps(specs[_key(LOC, "report")]) == {_key(LOC, "daily"), _key(LOC, "products")}
    assert _deps(specs[_key(LOC, "customers")]) == set() and _deps(specs[_key(LOC, "products")]) == set()


def test_the_job_links_are_not_the_data_links():
    projects = [_p("a", "alpha", data=["b"]), _p("b", "beta", ups=["a"])]
    spec_a, spec_b = (s for _, s in sorted(_specs(location_name(projects[0]), projects).items(), key=lambda kv: kv[0].path[-1]))
    assert _deps(spec_a) == set()                                       # alpha READS beta's data, but is not below beta as a job
    assert _deps(spec_b) == {_key(location_name(projects[0]), "alpha")}
    assert {s.key for s in project_asset_specs(location_name(projects[0]), projects)} == {AssetKey([location_name(projects[0]), "alpha"]),
                                                                                         AssetKey([location_name(projects[0]), "beta"])}


def test_a_link_to_a_job_in_another_repository_points_at_its_key():
    assert _deps(_specs(FIN)[_key(FIN, "ledger")]) == {_key(LOC, "report")}


def test_two_repositories_may_have_a_job_of_the_same_name():
    other = _p("z", "customers", git_url=OTHER)
    keys = job_lineage_asset_specs(LOC, [*PROJECTS, other])[0].key, job_lineage_asset_specs(location_name(other), [*PROJECTS, other])[0].key
    assert keys[0] != keys[1]


def test_each_node_describes_its_job():
    spec = _specs()[_key(LOC, "daily")]
    assert spec.metadata["project_id"] == "d" and spec.metadata["engine"] == "trino" and spec.metadata["git_url"] == REPO
    assert spec.metadata["schedule_cron"] == "*/5 * * * *" and spec.metadata["upstream_jobs"] == f"{PROJECTS[0]['name']}"
    assert "SQL job" in spec.description and "Upstream Jobs" in spec.description
    assert _specs()[_key(LOC, "customers")].metadata["schedule_cron"] == "" and _specs()[_key(LOC, "customers")].metadata["upstream_jobs"] == "none"
    assert "customers" in _specs()[_key(LOC, "customers")].description


def test_a_switched_off_schedule_is_not_shown():
    off = {**_p("o", "off", cron="*/5 * * * *"), "schedule_enabled": False}
    assert job_lineage_asset_specs(location_name(off), [off])[0].metadata["schedule_cron"] == ""


def test_a_project_without_a_kind_is_a_dbt_job():
    legacy = {k: v for k, v in _p("a", "alpha").items() if k != "kind"}
    assert job_lineage_asset_specs(location_name(legacy), [legacy])[0].metadata["kind"] == "dbt"


def test_unknown_and_self_upstream_jobs_are_ignored():
    assert job_upstream([_p("a", "alpha", ["a", "gone"])]) == {}


def test_a_cycle_of_job_links_is_broken_identically_in_every_location_and_the_location_still_loads():
    a, b = _p("a", "alpha", ["b"]), _p("b", "beta", ["a"], git_url=OTHER)
    kept = job_upstream([a, b])
    assert kept == {"b": ["a"]} and job_upstream([b, a]) == kept
    for project in (a, b):
        build_location_defs(location_name(project), [a, b]).resolve_asset_graph()


def test_the_code_location_loads_with_both_groups():
    defs = build_location_defs(LOC, ALL)
    graph = defs.resolve_asset_graph()
    assert graph.get(_key(LOC, "report")).parent_keys == {_key(LOC, "daily"), _key(LOC, "products")}
    assert graph.get(_key(LOC, "daily")).group_name == "job_lineage"
    data_keys = {AssetKey([LOC, "customers"]), AssetKey([LOC, "products"])}            # the data lineage: dbt projects and SQL jobs, not Python jobs
    keys = set(graph.get_all_asset_keys())
    assert data_keys <= keys and AssetKey([LOC, "daily"]) in keys and AssetKey([LOC, "report"]) not in keys
    assert {graph.get(k).group_name for k in data_keys} == {"inaiera_dbt_test_" + LOC.rsplit("-", 1)[-1]}


def test_a_job_link_pointing_across_repositories_loads_in_both_locations():
    finance = build_location_defs(FIN, ALL).resolve_asset_graph()
    assert finance.get(_key(FIN, "ledger")).parent_keys == {_key(LOC, "report")}      # the arrow points at the other repository's node
    home = build_location_defs(LOC, ALL).resolve_asset_graph()
    assert _key(FIN, "ledger") not in set(home.get_all_asset_keys())                  # each location defines its own jobs only


def test_a_change_in_job_links_reloads_the_location():
    before = location_fingerprint(PROJECTS)
    relinked = [*PROJECTS[:3], _p("r", "jobs/report.py", ["d"], kind="python")]
    assert location_fingerprint(relinked) != before
    assert location_fingerprint(list(reversed(PROJECTS))) == before


def test_a_schedule_change_still_does_not_reload_the_location():
    changed = [{**p, "schedule_cron": "0 * * * *"} for p in PROJECTS]
    assert location_fingerprint(changed) == location_fingerprint(PROJECTS)


# ── a successful run turns the job's node green ──────────────────────────────────────────────────────────────────────────
def _report(kind, status="succeeded", job="daily", location=LOC):
    from dagster import DagsterEventType, DagsterInstance, EventRecordsFilter

    from pralai_dbt_runner_dags.jobs.dbt_run_job import dbt_run_result_job, make_project_job
    from tests.test_asset_lineage import _Runner

    instance, runner = DagsterInstance.ephemeral(), _Runner(status)
    sub = make_project_job(job, "p1", f"jobs/{job}", location, kind=kind).execute_in_process(
        instance=instance, resources={"dbt_runner": runner}).run_id
    dbt_run_result_job.execute_in_process(instance=instance, resources={"dbt_runner": runner}, raise_on_error=False,
                                          run_config={"ops": {"report_run_result_op": {"config": {"run_id": "run-1", "submitted_by": sub}}}})
    records = instance.get_event_records(EventRecordsFilter(event_type=DagsterEventType.ASSET_MATERIALIZATION))
    return {r.event_log_entry.dagster_event.event_specific_data.materialization.asset_key:
            r.event_log_entry.dagster_event.event_specific_data.materialization for r in records}


def test_a_successful_job_of_every_kind_records_its_job_node():
    for kind in ("dbt", "sql", "python"):
        assert _key(LOC, "daily") in _report(kind), kind


def test_the_recorded_node_is_the_node_the_lineage_page_draws():
    project = {**_p("p1", "jobs/daily.sql", kind="sql")}
    drawn = {s.key for s in job_lineage_asset_specs(LOC, [project])}
    assert set(k for k in _report("sql", job="daily") if k.path[0] == "job_lineage") == drawn


def test_the_job_node_records_which_run_built_it():
    mat = _report("python", job="daily")[_key(LOC, "daily")]
    assert mat.metadata["dbt_run_id"].value == "run-1" and mat.metadata["submitted_by_dagster_run"].value
    assert mat.metadata["duration_seconds"].value == 4.2


def test_a_failed_or_cancelled_job_records_nothing_and_a_dbt_job_also_keeps_its_data_node():
    for status in ("failed", "cancelled"):
        assert _report("sql", status) == {}
    from pralai_dbt_runner_dags.assets import project_asset_key
    assert set(_report("dbt", job="customers")) == {_key(LOC, "customers"), project_asset_key(LOC, "customers")}


def test_the_generic_job_records_no_node():
    from pralai_dbt_runner_dags import monitoring
    assert _report("dbt", location=monitoring.GENERIC_LOCATION) == {}


# ── SQL jobs are in the DATA lineage too ─────────────────────────────────────────────────────────────────────────────────
def _data_specs(projects, location=LOC):
    return {s.key: s for s in project_asset_specs(location, projects)}


def test_a_sql_job_is_a_data_node_and_a_python_job_is_not():
    specs = _data_specs(ALL)
    assert AssetKey([LOC, "daily"]) in specs and AssetKey([LOC, "report"]) not in specs
    sql = specs[AssetKey([LOC, "daily"])]
    assert sql.group_name == specs[AssetKey([LOC, "customers"])].group_name and "SQL job" in sql.description and sql.metadata["project_id"] == "d"
    assert "dagster/kind/sql" in sql.tags and "dagster/kind/dbt" in specs[AssetKey([LOC, "customers"])].tags


def test_a_sql_jobs_data_arrows_come_from_the_data_dependencies_the_runner_reports():
    projects = [_p("c", "customers"), _p("d", "jobs/daily.sql", kind="sql", data=["c"]), _p("x", "exec", data=["d"])]
    specs = _data_specs(projects, location_name(projects[0]))
    loc = location_name(projects[0])
    assert {k.path[-1]: {d.asset_key.path[-1] for d in s.deps} for k, s in specs.items()} == {"customers": set(), "daily": {"customers"}, "exec": {"daily"}}
    assert loc == LOC


def test_the_job_links_and_the_data_links_stay_separate_for_a_sql_job():
    projects = [_p("c", "customers"), _p("p", "products"), _p("d", "jobs/daily.sql", ["p"], kind="sql", data=["c"])]
    loc = location_name(projects[0])
    data = {d.asset_key.path[-1] for d in _data_specs(projects, loc)[AssetKey([loc, "daily"])].deps}
    job = {d.asset_key.path[-1] for s in job_lineage_asset_specs(loc, projects) if s.key.path[-1] == "daily" for d in s.deps}
    assert data == {"customers"} and job == {"products"}


def test_a_change_in_a_sql_jobs_data_dependencies_reloads_the_location():
    before = [_p("c", "customers"), _p("d", "jobs/daily.sql", kind="sql", data=["c"])]
    after = [_p("c", "customers"), _p("d", "jobs/daily.sql", kind="sql", data=[])]
    assert location_fingerprint(before) != location_fingerprint(after)

