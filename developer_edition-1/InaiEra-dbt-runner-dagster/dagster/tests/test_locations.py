"""Per-repo code locations: naming, location planning, jobs and sensors."""
from __future__ import annotations

import json

import pytest
import yaml

from dagster import build_sensor_context

from inaiera_dbt_runner_dags import location_manager
from inaiera_dbt_runner_dags.location_defs import build_location_defs
from inaiera_dbt_runner_dags.naming import (
    build_job_names, canonical_repo_url, group_by_location, location_fingerprint, location_label, location_name, repo_hash,
    repo_name,
)
from inaiera_dbt_runner_dags.sensors import git_sensor

URL = "https://github.com/acme/inaiera-dbt-test.git"
LOC = location_name({"git_url": URL})                 # DG-012: "<repo name>-<hash of the canonical URL>"


def proj(pid, path="", url=URL, cron=None, sha=None, git=False):
    return {"git_trigger_enabled": git, "id": pid, "name": f"inaiera-dbt-test/{path}" if path else "inaiera-dbt-test",
            "git_url": url, "git_ref": "main", "project_path": path, "schedule_cron": cron,
            "updated_at": "2026-01-01T00:00:00+00:00", "last_synced_sha": sha}


class FakeRunner:
    def __init__(self, projects):
        self._projects = projects

    def list_scheduled_projects(self):
        return [p for p in self._projects if p["schedule_cron"]]

    def list_git_triggered_projects(self):
        return [p for p in self._projects if p.get("git_trigger_enabled")]

    head = "new"                                          # what the runner says the ref points to (DG-009)

    def get_project_head(self, project_id):
        return self.head


# ---- naming ----------------------------------------------------------------
def test_repo_name():
    assert repo_name("https://github.com/acme/inaiera-dbt-test.git") == "inaiera-dbt-test"
    assert repo_name("https://github.com/acme/repo/") == "repo"
    assert repo_name("git@github.com:acme/repo.git") == "repo"
    assert repo_name("https://github.com/acme/repo.git#subdirectory=x") == "repo"
    assert repo_name("") == "repo"


def test_job_names_are_dagster_safe_and_unique():
    names = build_job_names([proj("1", "customers"), proj("2", "my-project"), proj("3", "3d"),
                             proj("4", "a/sales"), proj("5", "b/sales"), proj("6", "")])
    assert names["1"] == "customers"
    assert names["2"] == "my_project"
    assert names["3"] == "p_3d"
    assert names["4"] == "a__sales" and names["5"] == "b__sales"   # same folder name -> whole path
    assert names["6"] == "inaiera_dbt_test"                         # repo-root project -> repo name
    assert len(set(names.values())) == 6


def test_group_and_fingerprint():
    a, b = proj("1", "customers"), proj("2", "products")
    other = proj("3", "x", url="https://github.com/acme/other.git")
    groups = group_by_location([a, b, other])
    assert sorted(groups) == sorted([LOC, location_name(other)])
    assert location_fingerprint(groups[LOC]) != location_fingerprint([a])
    # a schedule change must NOT change the fingerprint (sensors read schedules live)
    assert location_fingerprint([a]) == location_fingerprint([{**a, "schedule_cron": "*/5 * * * *"}])


# ---- manager planning ------------------------------------------------------
def test_plan_changes():
    running = {"a": (1,), "b": (2,), "gone": (3,)}
    alive = {"a": True, "b": False, "gone": True}
    desired = {"a": (1,), "b": (2,), "c": (4,)}
    assert location_manager.plan_changes(running, alive, desired) == (["c"], ["b"], ["gone"])
    # fingerprint change restarts a live server
    assert location_manager.plan_changes({"a": (1,)}, {"a": True}, {"a": (9,)}) == ([], ["a"], [])


def test_render_workspace():
    text = location_manager.render_workspace("dagster-code", [("inaiera-dbt-runner-dags", 4000), (LOC, 4001)])
    doc = yaml.safe_load(text)
    assert doc["load_from"][1] == {"grpc_server": {"host": "dagster-code", "port": 4001, "location_name": LOC}}
    assert text.count("grpc_server") == 2


# ---- definitions -----------------------------------------------------------
def test_location_defs_one_job_per_project_five_sensors_and_the_result_job():
    defs = build_location_defs(LOC, [proj("1", "customers"), proj("2", "products"),
                                     proj("3", "x", url="https://github.com/acme/other.git")])
    assert sorted(j.name for j in defs.jobs) == ["customers", "dbt_run_result", "products"]    # + the DG-018 result job
    assert sorted(s.name for s in defs.sensors) == ["dbt_run_monitor_sensor", "dependency_failure_sensor", "dependency_success_sensor", "git_commit_sensor", "project_schedule_sensor"]
    # the project is the job's default config, so a manual launch needs nothing extra
    job = next(j for j in defs.jobs if j.name == "customers")
    assert job.run_config["ops"]["submit_run_op"]["config"]["project_id"] == "1"


# ---- sensors ---------------------------------------------------------------
def _sensors(projects):
    defs = build_location_defs(LOC, projects)
    return {s.name: s for s in defs.sensors}


def test_schedule_sensor_targets_each_projects_own_job():
    projects = [proj("1", "customers", cron="*/2 * * * *"), proj("2", "products", cron="*/5 * * * *"),
                proj("9", "x", url="https://github.com/acme/other.git", cron="*/2 * * * *")]
    sensor = _sensors(projects[:2])["project_schedule_sensor"]
    ctx = build_sensor_context(resources={"dbt_runner": FakeRunner(projects)})
    result = sensor(ctx)
    assert sorted(r.job_name for r in result.run_requests) == ["customers", "products"]   # 'other' repo ignored
    assert {r.tags["trigger"] for r in result.run_requests} == {"scheduled"}
    assert set(json.loads(result.cursor)) == {"1", "2"}


def test_project_without_a_job_yet_is_skipped_and_cursor_untouched():
    known = [proj("1", "customers", cron="*/2 * * * *")]
    new = proj("2", "products", cron="*/5 * * * *")        # registered after the location loaded
    sensor = _sensors(known)["project_schedule_sensor"]
    result = sensor(build_sensor_context(resources={"dbt_runner": FakeRunner(known + [new])}))
    assert [r.job_name for r in result.run_requests] == ["customers"]
    assert "2" not in json.loads(result.cursor)            # it will fire once the location reloads


def test_git_sensor_fires_only_on_a_new_commit(monkeypatch):
    projects = [proj("1", "customers", cron="*/2 * * * *", sha="old", git=True),
                proj("2", "products", cron="*/5 * * * *", sha="new", git=True)]
    sensor = _sensors(projects)["git_commit_sensor"]
    result = sensor(build_sensor_context(resources={"dbt_runner": FakeRunner(projects)}))
    assert [r.job_name for r in result.run_requests] == ["customers"]       # products is already at 'new'
    assert result.run_requests[0].tags["trigger"] == "git_push"


def test_render_workspace_with_no_repos_is_valid_and_empty():
    text = location_manager.render_workspace("dagster-code", [])
    assert "load_from: []" in text and "grpc_server" not in text


# ── DG-008: the git sensor's projects come from the commit-trigger listing, not the cron schedule ──────────────
def _git_tick(projects, monkeypatch):
    sensor = _sensors(projects)["git_commit_sensor"]
    return sensor(build_sensor_context(resources={"dbt_runner": FakeRunner(projects)})).run_requests


def test_a_project_with_a_commit_trigger_and_no_schedule_is_watched(monkeypatch):
    projects = [proj("1", "customers", cron=None, sha="old", git=True)]
    assert [r.job_name for r in _git_tick(projects, monkeypatch)] == ["customers"]


def test_a_scheduled_project_without_the_commit_trigger_is_not_watched(monkeypatch):
    projects = [proj("1", "customers", cron="*/2 * * * *", sha="old", git=False)]
    assert _git_tick(projects, monkeypatch) == []


def test_the_sensor_never_asks_for_the_scheduled_listing(monkeypatch):
    class OnlyGit(FakeRunner):
        def list_scheduled_projects(self):
            raise AssertionError("the git sensor must not use the scheduled listing")

    projects = [proj("1", "customers", sha="old", git=True)]
    sensor = _sensors(projects)["git_commit_sensor"]
    assert len(sensor(build_sensor_context(resources={"dbt_runner": OnlyGit(projects)})).run_requests) == 1


def test_the_cron_sensor_is_unchanged_by_the_commit_trigger():
    projects = [proj("1", "customers", cron="*/2 * * * *", git=False), proj("2", "products", cron=None, git=True)]
    sensor = _sensors(projects)["project_schedule_sensor"]
    result = sensor(build_sensor_context(resources={"dbt_runner": FakeRunner(projects)}))
    assert [r.job_name for r in result.run_requests] == ["customers"]       # only the scheduled project fires from cron


# ── DG-012: a location's identity is its repository, not its base name ───────────────────────────────────────
def test_two_repositories_with_the_same_base_name_are_two_locations():
    a = proj("1", "sales", url="https://github.com/orgA/analytics.git")
    b = proj("2", "sales", url="https://github.com/orgB/analytics.git")
    assert repo_name(a["git_url"]) == repo_name(b["git_url"]) == "analytics"           # the OLD identity: a collision
    assert location_name(a) != location_name(b)
    groups = group_by_location([a, b])
    assert len(groups) == 2 and all(len(ps) == 1 for ps in groups.values())          # never mixed in one code server
    assert location_label(a) == location_label(b) == "analytics"                      # the readable part is only a label


def test_the_same_base_name_on_a_different_host_is_a_different_location():
    assert location_name(proj("1", url="https://github.com/acme/analytics.git")) != location_name(proj("1", url="https://gitlab.com/acme/analytics.git"))


@pytest.mark.parametrize("spelling", [
    "https://github.com/acme/analytics.git", "https://github.com/acme/analytics", "https://github.com/acme/analytics/",
    "HTTPS://GitHub.com/acme/analytics.git", "https://github.com:443/acme/analytics.git", "git@github.com:acme/analytics.git",
    "ssh://git@github.com/acme/analytics.git", "https://ci:s3cret@github.com/acme/analytics.git", "https://github.com/acme/analytics.git#subdirectory=sales",
    "git@github.com:acme/analytics.git#subdirectory=sales",
])
def test_every_spelling_of_one_repository_is_one_location(spelling):
    assert location_name(proj("1", url=spelling)) == location_name(proj("1", url="https://github.com/acme/analytics.git"))


def test_credentials_never_influence_or_appear_in_the_name():
    name = location_name(proj("1", url="https://ci:s3cret@github.com/acme/analytics.git"))
    assert "s3cret" not in name
    assert name == location_name(proj("1", url="https://github.com/acme/analytics.git"))


def test_the_path_is_case_sensitive_so_differently_cased_repositories_stay_apart():
    assert repo_hash("https://git.example/Team/Analytics.git") != repo_hash("https://git.example/team/analytics.git")        # the hash, not just the label


def test_a_non_default_port_is_part_of_the_identity():
    assert location_name(proj("1", url="https://git.example:8443/a/b.git")) != location_name(proj("1", url="https://git.example/a/b.git"))


def test_the_name_is_stable_and_dagster_safe():
    name = location_name(proj("1", url="https://github.com/acme/My Repo!.git"))
    assert name == location_name(proj("1", url="https://github.com/acme/My Repo!.git"))        # deterministic (no randomness, no process state)
    import re
    assert re.fullmatch(r"[A-Za-z0-9_.-]+-[0-9a-f]{8}", name), name
    assert repo_hash("https://github.com/acme/analytics.git") == repo_hash("git@github.com:acme/analytics.git")


def test_the_hash_is_pinned_so_a_refactor_cannot_silently_rename_every_location():
    """A location's name is the key of its Dagster run history, so the algorithm is part of the contract."""
    assert canonical_repo_url("https://github.com/acme/analytics.git") == "github.com/acme/analytics"
    assert repo_hash("https://github.com/acme/analytics.git") == "6bf3fd86"
    assert location_name({"git_url": "https://github.com/acme/analytics.git"}) == "analytics-6bf3fd86"


def test_a_project_with_no_url_still_gets_a_name():
    assert location_name({"git_url": ""}).startswith("repo-")


# ── DG-013: the generated workspace.yaml is a serialised object, never interpolated text ─────────────────────────────
NASTY = ['a"b', "a'b", "a\\b", "a: b", "a #b", "- a", "a\nb", "{a: b}", "[a]", "yes", "null", "123", "é", "a\tb", "", "  ", "%x", "!tag", "&anchor", "*alias"]


@pytest.mark.parametrize("name", NASTY)
def test_a_hostile_location_name_still_gives_valid_unambiguous_yaml(name):
    text = location_manager.render_workspace("dagster-code", [(name, 4001), ("ok-12345678", 4002)])
    doc = yaml.safe_load(text)                                                     # parses, with no exception ...
    assert [e["grpc_server"]["port"] for e in doc["load_from"]] == [4001, 4002]    # ... and keeps exactly two entries
    written = doc["load_from"][0]["grpc_server"]["location_name"]
    assert isinstance(written, str) and written == location_manager.sanitize_location_name(name)
    assert location_manager._LOCATION_NAME.match(written)                          # and the name is within Dagster's naming rules
    assert set(doc) == {"load_from"} and set(doc["load_from"][0]["grpc_server"]) == {"host", "port", "location_name"}   # nothing injected


def test_a_name_that_is_already_valid_is_unchanged():
    for name in ("inaiera-dbt-test-74325b4e", "analytics-6bf3fd86", "a.b_c-d"):
        assert location_manager.sanitize_location_name(name) == name


def test_sanitizing_keeps_names_distinct_enough_that_a_collision_is_refused_not_silent():
    with pytest.raises(ValueError, match="two code locations"):
        location_manager.render_workspace("h", [("a b", 4001), ("a:b", 4002)])        # both become "a-b"


@pytest.mark.parametrize("port", [0, -1, 70000, "4001", 4001.5, True])
def test_an_invalid_port_is_refused(port):
    with pytest.raises(ValueError, match="port"):
        location_manager.render_workspace("h", [("ok-12345678", port)])


@pytest.mark.parametrize("host", ["", None, 5])
def test_an_invalid_host_is_refused(host):
    with pytest.raises(ValueError, match="host"):
        location_manager.render_workspace(host, [("ok-12345678", 4001)])


def test_a_hostile_host_cannot_inject_structure():
    text = location_manager.render_workspace("evil\"\n  - grpc_server: {host: x}", [("ok-12345678", 4001)])
    doc = yaml.safe_load(text)
    assert len(doc["load_from"]) == 1 and doc["load_from"][0]["grpc_server"]["host"] == "evil\"\n  - grpc_server: {host: x}"


def test_the_empty_workspace_is_valid():
    assert yaml.safe_load(location_manager.render_workspace("dagster-code", [])) == {"load_from": []}


def test_the_file_starts_with_the_generated_header_and_is_plain_yaml():
    text = location_manager.render_workspace("dagster-code", [("ok-12345678", 4001)])
    assert text.startswith("# GENERATED by location_manager.py")
    assert "!!python" not in text                                                  # safe_dump: no python-specific tags
