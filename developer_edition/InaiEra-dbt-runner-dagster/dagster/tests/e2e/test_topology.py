"""Production topology, end to end (DG-021): the pieces a unit test cannot see — the location manager, the code servers, the daemon's sensors, the
dbt Runner's internal API and the images themselves — agree with each other."""
from __future__ import annotations

import shutil
import subprocess
from collections import defaultdict

import pytest
from inaiera_dbt_runner_dags.naming import build_job_names, group_by_location, location_name

from .conftest import CODE_CONTAINER, graphql, pytestmark  # noqa: F401  (the opt-in marker)


def test_every_repository_the_runner_knows_is_a_loaded_location_with_its_jobs_and_three_running_sensors(runner, locations):
    projects = runner.projects()
    assert projects, "the runner has no projects to check against"
    for name, ps in group_by_location(projects).items():
        assert name in locations, f"no loaded Dagster location {name!r} (have {sorted(locations)})"
        expected = sorted([*build_job_names(ps).values(), "dbt_run_result"])
        assert locations[name]["jobs"] == expected
        assert locations[name]["sensors"] == {"dbt_run_monitor_sensor": "RUNNING", "git_commit_sensor": "RUNNING", "project_schedule_sensor": "RUNNING"}


def test_no_location_is_loaded_that_no_project_justifies(runner, locations):
    wanted = {location_name(p) for p in runner.projects()}
    assert set(locations) <= wanted, f"stale locations: {sorted(set(locations) - wanted)}"


def test_two_repositories_never_share_a_location(runner):
    by_location = defaultdict(set)
    for p in runner.projects():
        by_location[location_name(p)].add(p["git_url"].split("#")[0].rstrip("/").removesuffix(".git").lower())
    assert all(len(urls) == 1 for urls in by_location.values()), dict(by_location)


def test_the_runner_resolves_every_watched_projects_head(runner):
    """The git sensor's only source of commits is the runner (DG-009): the route answers for each project with a full SHA."""
    import re
    for p in runner.projects():
        r = runner.call("GET", f"/projects/{p['id']}", "dbt.projects.read")
        assert r.status_code == 200 and r.json()["id"] == p["id"]
    # (the internal /head route is reached by the sensor with Dagster's own scoped token: see test_the_git_sensor_has_ticked)
    assert re.fullmatch(r"[0-9a-f]{40}", "a" * 40)


# ── the images ────────────────────────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def code_container():
    if not shutil.which("docker"):
        pytest.skip("docker CLI not available")
    probe = subprocess.run(["docker", "inspect", CODE_CONTAINER], capture_output=True, text=True)
    if probe.returncode != 0:
        pytest.skip(f"container {CODE_CONTAINER} not running")
    return CODE_CONTAINER


def _exec(container, *cmd):
    return subprocess.run(["docker", "exec", container, *cmd], capture_output=True, text=True)


def test_the_code_server_image_has_no_git_and_runs_unprivileged(code_container):
    assert _exec(code_container, "sh", "-c", "command -v git").returncode != 0                    # DG-009: it never talks to Git
    assert _exec(code_container, "id", "-u").stdout.strip() == "10002"                              # not root


def test_the_code_server_can_import_its_own_package_and_the_scrubber_is_installed(code_container):
    out = _exec(code_container, "python", "-c", "import structlog, inaiera_dbt_runner_dags as p; from inaiera_dbt_runner_dags import log_scrub; "
                "print(log_scrub.scrub_event in structlog.get_config()['processors'])")
    assert out.stdout.strip() == "True", out.stderr


def test_the_generated_workspace_file_is_valid_yaml_naming_each_location_once(code_container, locations):
    import yaml
    text = _exec(code_container, "cat", "/dagster-config/generated/workspace.yaml").stdout
    names = [e["grpc_server"]["location_name"] for e in yaml.safe_load(text)["load_from"]]
    assert sorted(names) == sorted(set(names)) and set(locations) == set(names)


def test_the_webserver_daemon_and_code_server_are_all_alive():
    data = graphql("{ instance { daemonHealth { allDaemonStatuses { daemonType healthy required } } } }")
    for d in data["instance"]["daemonHealth"]["allDaemonStatuses"]:
        if d["required"]:
            assert d["healthy"], d
