"""Run now through Dagster.

The dependency rule (a job runs only when every job above it has a recent success) is enforced in Dagster, in the submit step of each
project's job (InaiEra-dbt-runner-dagster, dependencies.py). A run started by hand has to go through the same door, so when
`DAGSTER_GRAPHQL_URL` is set the Workspace's Run now LAUNCHES THE PROJECT'S DAGSTER JOB and waits for its submit step, which only hands
the run to the dbt Runner (seconds):

  * the step succeeded   -> the Runner's run (the Dagster run is tagged with its id) is returned, exactly as a direct call would;
  * the step was refused -> the reason ("Not started: upstream project 'a' failed ...") is returned as a 409, so the person who clicked
                            sees why; nothing was sent to the Runner;
  * Dagster is down      -> 503: Run now needs Dagster once it is configured, rather than quietly bypassing the rule.

The job is found by the `project_id` tag every project job carries (one job per project, in the code location of its repo); a Helm
deployment's generic job `dbt_scheduled_run` takes the project in its launch config instead.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import httpx
import structlog

from app.core.config import get_settings
from app.core.errors import WorkspaceError

log = structlog.get_logger()

TAG_PROJECT = "project_id"
TAG_RUNNER_RUN = "inaiera/dbt_run_id"
TAG_TRIGGERED_BY = "inaiera/triggered_by"
GENERIC_JOB = "dbt_scheduled_run"
TERMINAL = {"SUCCESS", "FAILURE", "CANCELED"}
POLL_SECONDS = 0.5

_JOBS = """{ workspaceOrError { __typename ... on Workspace { locationEntries { name loadStatus locationOrLoadError {
  __typename ... on RepositoryLocation { name repositories { name pipelines { name tags { key value } } } } } } } } }"""
_LAUNCH = """mutation($params: ExecutionParams!) { launchRun(executionParams: $params) { __typename
  ... on LaunchRunSuccess { run { runId } } ... on Error { message } } }"""
_RUN = """query($id: ID!) { runOrError(runId: $id) { __typename ... on Run { runId status tags { key value } } ... on Error { message } } }"""
_LOGS = """query($id: ID!) { logsForRun(runId: $id) { __typename ... on EventConnection { events { __typename
  ... on ExecutionStepFailureEvent { error { message className } } } } } }"""


@dataclass(frozen=True)
class JobRef:
    location: str
    repository: str
    job: str
    generic: bool = False


def _unavailable(detail: str = "") -> WorkspaceError:
    return WorkspaceError(503, "DAGSTER_UNAVAILABLE",
                          "Dagster is not reachable, and Run now starts runs through Dagster so that job dependencies are enforced. "
                          "Try again when Dagster is back." + (f" ({detail})" if detail else ""))


async def _graphql(client: httpx.AsyncClient, query: str, variables: dict | None = None) -> dict:
    try:
        resp = await client.post(get_settings().dagster_graphql_url, json={"query": query, "variables": variables or {}})
        resp.raise_for_status()
        body = resp.json()
    except (httpx.TransportError, httpx.HTTPStatusError, ValueError) as e:
        log.error("dagster_unreachable", error=type(e).__name__)
        raise _unavailable(type(e).__name__) from e
    if body.get("errors"):
        log.error("dagster_graphql_error", errors=[e.get("message") for e in body["errors"]][:3])
        raise WorkspaceError(502, "DAGSTER_ERROR", "Dagster rejected the request.")
    return body["data"]


async def find_job(client: httpx.AsyncClient, project_id: str) -> JobRef:
    """The Dagster job of `project_id`: its own job (tagged with the project) or, failing that, the generic job."""
    entries = (await _graphql(client, _JOBS))["workspaceOrError"].get("locationEntries") or []
    generic: JobRef | None = None
    loading = 0
    for entry in entries:
        location = entry.get("locationOrLoadError") or {}
        if location.get("__typename") != "RepositoryLocation":
            loading += 1
            continue
        for repo in location.get("repositories") or []:
            for job in repo.get("pipelines") or []:
                tags = {t["key"]: t["value"] for t in job.get("tags") or []}
                if tags.get(TAG_PROJECT) == project_id:
                    return JobRef(location["name"], repo["name"], job["name"])
                if job["name"] == GENERIC_JOB:
                    generic = JobRef(location["name"], repo["name"], job["name"], generic=True)
    if generic is not None:
        return generic
    hint = " Dagster is still loading some repositories; try again in a minute." if loading else \
        " Dagster adds a job within about a minute of a project being registered."
    raise WorkspaceError(503, "DAGSTER_JOB_NOT_FOUND", "Dagster has no job for this project yet." + hint)


async def _launch(client: httpx.AsyncClient, job: JobRef, project_id: str, triggered_by: str) -> str:
    params: dict = {
        "selector": {"repositoryLocationName": job.location, "repositoryName": job.repository, "jobName": job.job},
        "runConfigData": ({"ops": {"submit_run_op": {"config": {"project_id": project_id}}}} if job.generic else {}),
        "executionMetadata": {"tags": [{"key": TAG_TRIGGERED_BY, "value": triggered_by}]},
    }
    result = (await _graphql(client, _LAUNCH, {"params": params}))["launchRun"]
    if result["__typename"] != "LaunchRunSuccess":
        log.error("dagster_launch_refused", kind=result["__typename"], message=result.get("message"))
        raise WorkspaceError(502, "DAGSTER_LAUNCH_FAILED", f"Dagster could not start the run: {result.get('message') or result['__typename']}")
    return result["run"]["runId"]


async def _wait(client: httpx.AsyncClient, run_id: str) -> dict:
    deadline = time.monotonic() + get_settings().dagster_run_wait_seconds
    while True:
        run = (await _graphql(client, _RUN, {"id": run_id}))["runOrError"]
        if run["__typename"] == "Run" and run["status"] in TERMINAL:
            return run
        if time.monotonic() >= deadline:
            raise WorkspaceError(504, "DAGSTER_RUN_TIMEOUT",
                                 "Dagster accepted the run but has not handed it to the runner yet. Check the Runs tab in a moment.")
        await asyncio.sleep(POLL_SECONDS)


async def _failure_reason(client: httpx.AsyncClient, run_id: str) -> tuple[str, bool]:
    """(why the Dagster run failed, whether it was a refusal by the dependency rule — a Dagster `Failure` — rather than an error)."""
    logs = (await _graphql(client, _LOGS, {"id": run_id}))["logsForRun"]
    for event in logs.get("events") or []:
        error = event.get("error") if event.get("__typename") == "ExecutionStepFailureEvent" else None
        if error:
            message = (error.get("message") or "").strip()
            refused = (error.get("className") or "").endswith("Failure")
            if refused and message.startswith("dagster._core.definitions.events.Failure:"):
                message = message.split(":", 1)[1].strip()
            return message, refused
    return "", False


async def run_now(project_id: str, triggered_by: str) -> str:
    """Launch the project's Dagster job and return the dbt Runner's run id once Dagster has handed the run over.
    Raises WorkspaceError: 409 UPSTREAM_NOT_READY (refused by the dependency rule), 503 Dagster unavailable / no job yet,
    502 the run failed for another reason, 504 not handed over in time."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
        job = await find_job(client, project_id)
        run_id = await _launch(client, job, project_id, triggered_by)
        run = await _wait(client, run_id)
        tags = {t["key"]: t["value"] for t in run.get("tags") or []}
        if run["status"] == "SUCCESS":
            runner_run = tags.get(TAG_RUNNER_RUN)
            if not runner_run:
                raise WorkspaceError(502, "DAGSTER_RUN_FAILED", "Dagster ran the job but did not report the runner's run.")
            return runner_run
        reason, refused = await _failure_reason(client, run_id)
        log.warning("dagster_run_now_failed", project_id=project_id, dagster_run=run_id, status=run["status"], refused=refused)
        if refused:
            raise WorkspaceError(409, "UPSTREAM_NOT_READY", reason or "An upstream job is not ready.", {"dagster_run_id": run_id})
        raise WorkspaceError(502, "DAGSTER_RUN_FAILED", f"Dagster could not hand the run to the runner: {reason or run['status']}",
                             {"dagster_run_id": run_id})
