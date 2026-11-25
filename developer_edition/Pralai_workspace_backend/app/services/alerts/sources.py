"""Where the sweep learns what happened: the dbt Runner (dbt / SQL / Python runs), the FlinkSQL runner (job states) and Dagster
(runs it refused because an upstream job was not ready).

Each source returns what it saw as `Obs` and NEVER raises: a source that is down is reported as not ok, and the sweep carries on with
the others. A runner being unreachable is not itself a job failure, so it raises no alert.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx
import structlog

from app.core.auth import Principal
from app.core.config import get_settings
from app.core.service_tokens import (
    DBT_SCOPE_PROJECTS_READ, DBT_SCOPE_RUNS_READ, DBTRUNNER_AUDIENCE, FLINK_AUDIENCE, FLINK_SCOPE_JOBS_READ, delegated_authorization,
)
from app.services.alerts.engine import Obs, reason_text

log = structlog.get_logger()

SYSTEM_SUBJECT = "workspace-alerts"
RUNS_PER_PROJECT = 50
CONCURRENCY = 6
DAGSTER_RUNS_LOOKBACK = 100
# What dbt says when a data test / quality check did not pass ("Got 3 results, configured to fail if != 0").
_TEST_FAILURE = re.compile(r"Got \d+ results?, configured to (?:fail|warn) if", re.I)
# The Dagster start check's own wording (Pralai-dbt-runner-dagster, dependencies.reason_text).
_REFUSED = "Not started:"
TAG_PROJECT = "project_id"


@dataclass
class SourceResult:
    source: str
    ok: bool = True
    detail: str = ""
    observations: list[Obs] = field(default_factory=list)


# What the sweep may ask of the runners: read job lists and run history. Each delegated token carries only the permissions its
# scope needs out of this set (app/core/service_tokens.SCOPE_PERMISSIONS), and every call it makes is a read scope.
SYSTEM_PERMISSIONS = frozenset({
    "workspace:dbt:run", "workspace:sql-jobs:run", "workspace:python-jobs:run", "workspace:flink:edit",
})


def system_principal() -> Principal:
    """Who the sweep acts as towards the runners: a read-only service identity, not a person."""
    return Principal(user_id=SYSTEM_SUBJECT, org_id=get_settings().org_id, roles=[], email=None, issued_at=0, expires_at=0,
                     permissions=SYSTEM_PERMISSIONS)


def _why(e: Exception) -> str:
    """A short, safe reason for an unexpected failure: a WorkspaceError's code, else the exception's name."""
    detail = getattr(e, "detail", None)
    return str(detail.get("code")) if isinstance(detail, dict) and detail.get("code") else type(e).__name__


def _parse_dt(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, str) and value:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


def run_observation(project: dict, run: dict) -> Obs | None:
    """One finished run of a dbt / SQL / Python project (None while it is still queued or running)."""
    status = str(run.get("status") or "")
    if status not in ("succeeded", "failed", "cancelled"):
        return None
    when = _parse_dt(run.get("completed_at")) or _parse_dt(run.get("started_at")) or _parse_dt(run.get("created_at"))
    if when is None:
        return None
    message = run.get("error_message") if status == "failed" else None
    return Obs(
        job_key=str(project["id"]), job_name=str(project.get("name") or project["id"]), job_kind=str(project.get("kind") or "dbt"),
        category="run", outcome=status, occurred_at=when, ref_id=str(run["id"]),
        failure_class="quality" if message and _TEST_FAILURE.search(message) else None, detail=message,
    )


async def _get_json(client: httpx.AsyncClient, url: str, authorization: str, params: dict | None = None):
    resp = await client.get(url, headers={"Authorization": authorization}, params=params)
    resp.raise_for_status()
    return resp.json()


async def collect_runner(known_refs: set[str], *, client: httpx.AsyncClient | None = None) -> tuple[SourceResult, dict[str, tuple[str, str]]]:
    """Runs of every dbt / SQL / Python project. A project whose latest run is already known has nothing new, so its run list is not
    fetched (one call per project that changed, not per project). Also returns {project id: (name, kind)} for the other sources."""
    s = get_settings()
    result = SourceResult("dbt-runner")
    names: dict[str, tuple[str, str]] = {}
    base = f"{s.dbtrunner_url}/api/v1"
    own = client is None
    client = client or httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=s.upstream_connect_timeout_seconds))
    try:
        auth_list = delegated_authorization(system_principal(), audience=DBTRUNNER_AUDIENCE, scope=DBT_SCOPE_PROJECTS_READ)
        projects = await _get_json(client, f"{base}/projects", auth_list)
        for p in projects:
            names[str(p["id"])] = (str(p.get("name") or p["id"]), str(p.get("kind") or "dbt"))
        stale = [p for p in projects if (p.get("last_run") or {}).get("id") and str(p["last_run"]["id"]) not in known_refs]
        gate = asyncio.Semaphore(CONCURRENCY)

        async def runs_of(p: dict) -> None:
            async with gate:
                try:
                    auth = delegated_authorization(system_principal(), audience=DBTRUNNER_AUDIENCE, scope=DBT_SCOPE_RUNS_READ)
                    runs = await _get_json(client, f"{base}/projects/{p['id']}/runs", auth, {"limit": RUNS_PER_PROJECT})
                except (httpx.HTTPError, ValueError) as e:
                    result.ok, result.detail = False, f"runs of '{p.get('name')}': {type(e).__name__}"
                    return
            for run in runs:
                obs = run_observation(p, run)
                if obs is not None:
                    result.observations.append(obs)

        await asyncio.gather(*(runs_of(p) for p in stale))
    except httpx.HTTPStatusError as e:
        result.ok, result.detail = False, f"the dbt Runner answered {e.response.status_code}"
    except (httpx.HTTPError, ValueError) as e:
        result.ok, result.detail = False, f"the dbt Runner could not be reached ({type(e).__name__})"
    except Exception as e:  # noqa: BLE001 - e.g. no delegation secret configured: report it, do not break the sweep
        result.ok, result.detail = False, f"the dbt Runner could not be asked ({_why(e)})"
    finally:
        if own:
            await client.aclose()
    return result, names


def flink_observation(job: dict) -> Obs | None:
    status = str(job.get("status") or "")
    when = _parse_dt(job.get("updated_at")) or _parse_dt(job.get("last_deployed_at")) or _parse_dt(job.get("created_at"))
    if not status or when is None:
        return None
    errors = job.get("validation_errors") or []
    return Obs(
        job_key=str(job["id"]), job_name=str(job.get("name") or job["id"]), job_kind="flink", category="flink_state", outcome=status,
        occurred_at=when, ref_id=f"{job['id']}:{status}:{when.isoformat()}",
        detail="; ".join(str(e) for e in errors)[:1000] if status == "failed" and errors else None,
    )


async def collect_flink(*, client: httpx.AsyncClient | None = None) -> SourceResult:
    s = get_settings()
    result = SourceResult("flink")
    own = client is None
    client = client or httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=s.upstream_connect_timeout_seconds))
    try:
        auth = delegated_authorization(system_principal(), audience=FLINK_AUDIENCE, scope=FLINK_SCOPE_JOBS_READ)
        jobs = await _get_json(client, f"{s.flinksql_runner_url}/api/v1/jobs", auth)
        result.observations = [o for o in map(flink_observation, jobs) if o is not None]
    except httpx.HTTPStatusError as e:
        result.ok, result.detail = False, f"the FlinkSQL runner answered {e.response.status_code}"
    except (httpx.HTTPError, ValueError) as e:
        result.ok, result.detail = False, f"the FlinkSQL runner could not be reached ({type(e).__name__})"
    except Exception as e:  # noqa: BLE001
        result.ok, result.detail = False, f"the FlinkSQL runner could not be asked ({_why(e)})"
    finally:
        if own:
            await client.aclose()
    return result


_DAGSTER_FAILED = """query($limit: Int!) { runsOrError(filter: {statuses: [FAILURE]}, limit: $limit) { __typename
  ... on Runs { results { runId jobName endTime tags { key value } } } ... on Error { message } } }"""
_DAGSTER_LOGS = """query($id: ID!) { logsForRun(runId: $id) { __typename ... on EventConnection { events { __typename
  ... on ExecutionStepFailureEvent { error { message } } } } } }"""


async def _dagster(client: httpx.AsyncClient, url: str, query: str, variables: dict) -> dict:
    resp = await client.post(url, json={"query": query, "variables": variables})
    resp.raise_for_status()
    body = resp.json()
    if body.get("errors"):
        raise ValueError("graphql error")
    return body["data"]


async def collect_dagster(known_refs: set[str], names: dict[str, tuple[str, str]], *,
                          client: httpx.AsyncClient | None = None) -> SourceResult:
    """Dagster runs that FAILED, kept as `blocked` when the failure is the dependency start check refusing the run
    ("Not started: upstream project 'a' failed ..."). Other Dagster failures are remembered (so they are not re-read every sweep) but
    raise nothing here: the dbt Runner's own failed run already does."""
    s = get_settings()
    result = SourceResult("dagster")
    if not s.dagster_graphql_url:
        result.detail = "not configured"
        return result
    own = client is None
    client = client or httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=s.upstream_connect_timeout_seconds))
    try:
        data = await _dagster(client, s.dagster_graphql_url, _DAGSTER_FAILED, {"limit": DAGSTER_RUNS_LOOKBACK})
        runs = (data.get("runsOrError") or {}).get("results") or []
        for run in runs:
            run_id = str(run["runId"])
            if run_id in known_refs:
                continue
            tags = {t["key"]: t["value"] for t in run.get("tags") or []}
            project_id = tags.get(TAG_PROJECT)
            if project_id is None or project_id not in names:
                continue
            when = _parse_dt(run.get("endTime"))
            if when is None:
                continue
            reason = ""
            try:
                logs = await _dagster(client, s.dagster_graphql_url, _DAGSTER_LOGS, {"id": run_id})
                for ev in (logs.get("logsForRun") or {}).get("events") or []:
                    msg = ((ev.get("error") or {}).get("message") or "") if ev.get("__typename") == "ExecutionStepFailureEvent" else ""
                    if _REFUSED in msg:
                        reason = msg[msg.index(_REFUSED):]
                        break
            except (httpx.HTTPError, ValueError):
                continue                       # try this run again next sweep
            name, kind = names[project_id]
            refused = bool(reason)
            result.observations.append(Obs(
                job_key=project_id, job_name=name, job_kind=kind, category="blocked" if refused else "dagster",
                outcome="blocked" if refused else "failed", occurred_at=when, ref_id=run_id, detail=reason_text(reason, 2_000) or None))
    except httpx.HTTPStatusError as e:
        result.ok, result.detail = False, f"Dagster answered {e.response.status_code}"
    except (httpx.HTTPError, ValueError) as e:
        result.ok, result.detail = False, f"Dagster could not be reached ({type(e).__name__})"
    finally:
        if own:
            await client.aclose()
    return result
