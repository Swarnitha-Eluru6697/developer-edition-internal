"""What the Lineage page draws: the projects' DATA dependencies and their JOB dependencies, as two groups of nodes.

DATA lineage — one asset per dbt project AND per SQL job, wired by the data it reads (below). A SQL job's reads and writes come from its own
statements, read by the Runner after a successful run (a best guess, see the Runner's data_dependencies.py); a Python job is not in it.

JOB lineage — one asset per job of ANY kind (dbt, SQL, Python) in the group ``job_lineage``, wired by the job links (a job's Upstream Jobs,
`upstream_project_ids`, set in Workspace and stored by the dbt Runner). An arrow runs from an upstream job to its downstream job. Same
project, different node: the group filter on the Lineage page picks one set or shows both, and the two sets are not connected to each
other. Key ``job_lineage/<code location>/<job name>`` (unique across repositories, like the data keys). The job nodes are only descriptions:
nothing records a run on them.

    inaiera-dbt-test-74325b4e/customers  ──▶  inaiera-dbt-test-74325b4e/products

  * key    ``<code location>/<job name>``: the same names the Jobs page uses, unique across every repository (DG-012).
  * deps   the projects whose data this one reads (`data_upstream_project_ids`, worked out by the dbt Runner from dbt's own lineage).
           Arrows mean data only: Dependents (job links) are not drawn.
  * group  the code location, so each repository's projects sit together.

The assets are only descriptions (no compute): a project is still built by its job. When a run Dagster submitted succeeds,
dbt_run_result records a materialization of the project's asset (ops/report_result.py), so the graph shows when each project last
built. Runs started from the Workspace do not go through Dagster and are not recorded here.

Dagster refuses a dependency cycle anywhere in the graph (the whole code location would fail to load), and two projects can read each
other's tables, so cycles are broken before the specs are built: the same edges are dropped in every location (logged).
"""
from __future__ import annotations

import re
from typing import Any

import structlog
from dagster import AssetKey, AssetSpec

from inaiera_dbt_runner_dags.naming import build_job_names, group_by_location

log = structlog.get_logger()

_GROUP_SAFE = re.compile(r"[^A-Za-z0-9_]")
_KIND_LABEL = {"dbt": "dbt project", "sql": "SQL job", "python": "Python job"}


JOB_LINEAGE_GROUP = "job_lineage"
DATA_LINEAGE_KINDS = ("dbt", "sql")          # the jobs that report which tables they read and write


def project_asset_key(location: str, job_name: str) -> AssetKey:
    return AssetKey([location, job_name])


def job_asset_key(location: str, job_name: str) -> AssetKey:
    return AssetKey([JOB_LINEAGE_GROUP, location, job_name])


def asset_group(location: str) -> str:
    """Dagster group names allow only letters, digits and '_'."""
    return _GROUP_SAFE.sub("_", location).strip("_") or "dbt"


def asset_keys(projects: list[dict[str, Any]]) -> dict[str, AssetKey]:
    """project id -> its asset key, for EVERY project (an upstream may live in another repository's location)."""
    keys: dict[str, AssetKey] = {}
    for location, members in group_by_location(projects).items():
        names = build_job_names(members)
        for p in members:
            keys[p["id"]] = project_asset_key(location, names[p["id"]])
    return keys


def job_asset_keys(projects: list[dict[str, Any]]) -> dict[str, AssetKey]:
    """project id -> its job-lineage key, for EVERY project (an upstream job may live in another repository's location)."""
    keys: dict[str, AssetKey] = {}
    for location, members in group_by_location(projects).items():
        names = build_job_names(members)
        for p in members:
            keys[p["id"]] = job_asset_key(location, names[p["id"]])
    return keys


def data_upstream(projects: list[dict[str, Any]]) -> dict[str, list[str]]:
    """project id -> the registered projects whose data it reads, with every cycle broken."""
    return _acyclic(projects, "data_upstream_project_ids")


def job_upstream(projects: list[dict[str, Any]]) -> dict[str, list[str]]:
    """project id -> its Upstream Jobs. The Runner refuses a loop of links, so nothing is dropped in practice; a cycle would stop the
    whole code location from loading, so one is broken here all the same."""
    return _acyclic(projects, "upstream_project_ids")


def _acyclic(projects: list[dict[str, Any]], field: str) -> dict[str, list[str]]:
    """project id -> the projects listed in its `field`, unknown and self edges ignored and every cycle broken.

    Projects are visited in a fixed order (name, id); an edge that would close a cycle with the edges already kept is dropped. Every
    location computes the same result from the same project list."""
    known = {p["id"] for p in projects}
    order = sorted(projects, key=lambda p: (p.get("name") or "", p["id"]))
    kept: dict[str, list[str]] = {}

    def reaches(src: str, dst: str) -> bool:           # is dst upstream of src through the kept edges?
        stack, seen = [src], set()
        while stack:
            node = stack.pop()
            if node == dst:
                return True
            if node not in seen:
                seen.add(node)
                stack.extend(kept.get(node, []))
        return False

    for p in order:
        for up in p.get(field) or []:
            if up not in known or up == p["id"]:
                continue
            if reaches(up, p["id"]):
                log.warning("asset_lineage.cycle_edge_dropped", project=p.get("name"), upstream=up, field=field)
                continue
            kept.setdefault(p["id"], []).append(up)
    return kept


def project_asset_specs(location: str, projects: list[dict[str, Any]]) -> list[AssetSpec]:
    """The assets of ONE location's projects; `projects` is every registered project (for upstream keys and cycles)."""
    keys = asset_keys(projects)
    upstream = data_upstream(projects)
    names = {p["id"]: p.get("name") or p["id"] for p in projects}
    specs = []
    for p in sorted(group_by_location(projects).get(location, []), key=lambda p: (p.get("name") or "", p["id"])):
        kind = p.get("kind", "dbt")
        if kind not in DATA_LINEAGE_KINDS:            # a Python job's SQL is in strings and variables: nothing to read
            continue
        what = "SQL job" if kind == "sql" else "dbt project"
        specs.append(AssetSpec(
            keys[p["id"]],
            deps=[keys[u] for u in upstream.get(p["id"], [])],
            group_name=asset_group(location),
            description=(f"{what[0].upper() + what[1:]} {names[p['id']]}. Run by its job in this location; arrows show the projects whose data it "
                         + ("reads (from dbt's lineage)." if kind == "dbt" else "reads and writes (from its own statements).")),
            metadata={"project_id": p["id"], "project": names[p["id"]], "git_url": p.get("git_url", ""),
                      "git_ref": p.get("git_ref", ""), "project_path": p.get("project_path") or ""},
            kinds={kind},
        ))
    return specs


def job_lineage_asset_specs(location: str, projects: list[dict[str, Any]]) -> list[AssetSpec]:
    """The job-lineage nodes of ONE location's jobs, of every kind; `projects` is every registered project (for upstream keys)."""
    keys = job_asset_keys(projects)
    upstream = job_upstream(projects)
    names = {p["id"]: p.get("name") or p["id"] for p in projects}
    job_names = {pid: key.path[-1] for pid, key in keys.items()}
    specs = []
    for p in sorted(group_by_location(projects).get(location, []), key=lambda p: (p.get("name") or "", p["id"])):
        kind = p.get("kind", "dbt")
        ups = upstream.get(p["id"], [])
        specs.append(AssetSpec(
            keys[p["id"]],
            deps=[keys[u] for u in ups],
            group_name=JOB_LINEAGE_GROUP,
            description=(f"Job {job_names[p['id']]} ({_KIND_LABEL.get(kind, kind)}). Arrows are job links: an upstream job points at "
                         "the downstream jobs that depend on it. Set them in Workspace (Upstream Jobs / Downstream Jobs)."),
            metadata={"project_id": p["id"], "project": names[p["id"]], "kind": kind, "engine": p.get("engine", ""),
                      "git_url": p.get("git_url", ""), "schedule_cron": (p.get("schedule_cron") or "") if p.get("schedule_enabled", True) else "",
                      "upstream_jobs": ", ".join(names[u] for u in ups) or "none"},
            kinds={kind},
        ))
    return specs
