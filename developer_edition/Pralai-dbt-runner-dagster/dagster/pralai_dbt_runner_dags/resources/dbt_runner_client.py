"""HTTP client resource for the InAiEra dbt Runner API.

Dagster ops and sensors use this resource instead of importing app.*
modules. That keeps the Dagster code server dependency-light (no dbt,
no asyncpg, no kubernetes SDK) and ensures a clean network boundary
between the orchestrator and the data-plane service.

Auth (DBT-003): Dagster has its OWN scoped identity at the runner. Every request carries a fresh,
short-lived HS256 JWT in ``X-Internal-Service-Token`` signed with SCHEDULER_JWT_SECRET (iss/sub "dagster",
aud "dbtrunner", this data plane's org, ONE scope for that call: dbt.projects.read, dbt.runs.read or
dbt.runs.trigger / dbt.runs.cancel). The runner's static INTERNAL_SERVICE_TOKEN (Workspace's codegen validate token) is never
sent and is no longer accepted on these routes. In production the secret is mounted from a Kubernetes
Secret into the code server pod.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from typing import Any

import httpx
import structlog
from dagster import ConfigurableResource

log = structlog.get_logger()

# How long to wait for the run API to respond (submit / poll)
_TIMEOUT_SECONDS = 30.0
_CANCEL_TIMEOUT_SECONDS = 10.0   # cancel runs while Dagster is terminating the step: it must answer promptly
_TERMINAL = {"succeeded", "failed", "cancelled"}


class RunPollTimeout(RuntimeError):
    """The polling ceiling was reached with the Runner's run still not finished."""


SCHEDULER_ISSUER = "dagster"
RUNNER_AUDIENCE = "dbtrunner"
TOKEN_TTL_SECONDS = 60   # the runner rejects anything longer than 300


def _b64(data: bytes) -> bytes:
    return base64.urlsafe_b64encode(data).rstrip(b"=")


def mint_scheduler_token(secret: str, org_id: str, scope: str, *, ttl_seconds: int = TOKEN_TTL_SECONDS) -> str:
    """One fresh HS256 token authorising exactly `scope` at the dbt Runner (never reuse one: the runner
    consumes a state-changing token's jti once). Standard library only; the Dagster image has no JWT lib."""
    if not secret:
        raise ValueError("SCHEDULER_JWT_SECRET is not set: Dagster cannot authenticate to the dbt Runner")
    now = int(time.time())
    head = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = _b64(json.dumps({
        "iss": SCHEDULER_ISSUER, "sub": SCHEDULER_ISSUER, "aud": RUNNER_AUDIENCE, "scope": scope,
        "org_id": org_id, "jti": uuid.uuid4().hex, "iat": now, "exp": now + ttl_seconds,
    }).encode())
    sig = _b64(hmac.new(secret.encode(), head + b"." + body, hashlib.sha256).digest())
    return (head + b"." + body + b"." + sig).decode()


class DbtRunnerClient(ConfigurableResource):
    """Thin HTTP wrapper around the dbt Runner REST API.

    Configure via Dagster resource config or environment variables.

    Attributes:
        base_url: Root URL of the dbt Runner service, without trailing
            slash. E.g. ``http://localhost:8082`` or
            ``http://dbt-runner.inaiera.svc``.
        scheduler_jwt_secret: Signing key shared with the dbt Runner's SCHEDULER_JWT_SECRET. Used to mint a
            scoped token per request, sent as the ``X-Internal-Service-Token`` header. Required.
        org_id: This data plane's org; the runner rejects a token for any other.
        timeout_seconds: Per-request HTTP timeout (default 30 s).
    """

    base_url: str
    scheduler_jwt_secret: str
    org_id: str
    timeout_seconds: float = _TIMEOUT_SECONDS

    def _headers(self, scope: str) -> dict[str, str]:
        return {
            "X-Internal-Service-Token": mint_scheduler_token(self.scheduler_jwt_secret, self.org_id, scope),
            "Content-Type": "application/json",
        }

    def _client(self, scope: str, timeout: float | None = None) -> httpx.Client:
        """A client whose one request carries a fresh token for `scope`."""
        return httpx.Client(
            base_url=self.base_url,
            headers=self._headers(scope),
            timeout=timeout or self.timeout_seconds,
        )

    # ------------------------------------------------------------------
    # Project queries
    # ------------------------------------------------------------------

    def list_projects(self) -> list[dict[str, Any]]:
        """Every registered project (used to build one Dagster job per project)."""
        with self._client("dbt.projects.read") as c:
            resp = c.get("/api/v1/internal/projects")
            resp.raise_for_status()
            return resp.json()

    def list_scheduled_projects(self) -> list[dict[str, Any]]:
        """Return all projects with schedule_enabled=true across all orgs.

        The Runner's /api/v1/internal/projects/scheduled endpoint is an internal-only
        route that bypasses per-org JWT auth — it accepts only Dagster's scoped
        scheduler token (dbt.projects.read). It lists projects for this data
        plane's org (single-tenant data plane model per TDD 1.3).
        """
        with self._client("dbt.projects.read") as c:
            resp = c.get("/api/v1/internal/projects/scheduled")
            resp.raise_for_status()
            return resp.json()

    def list_git_triggered_projects(self) -> list[dict[str, Any]]:
        """Projects set to run on every new commit (`git_trigger_enabled`, DG-008) — independent of any cron schedule.

        The git sensor's source. It is NOT the scheduled listing: a project can have a schedule and no commit trigger, or a commit
        trigger and no schedule. Same internal route family and scope (dbt.projects.read) as the other project listings; this data
        plane's org only.
        """
        with self._client("dbt.projects.read") as c:
            resp = c.get("/api/v1/internal/projects/git-triggered")
            resp.raise_for_status()
            return resp.json()

    def get_project_head(self, project_id: str) -> str:
        """The commit the project's ref points to right now, resolved BY THE RUNNER (DG-009).

        The runner holds the repository credentials (credential_ref) and the Git egress; Dagster has neither, so it never runs
        `git ls-remote` itself. Raises on any HTTP error (404 unknown project, 422 GIT_REF_NOT_FOUND, 502 GIT_UNREACHABLE): the
        caller treats it as "not known this tick".
        """
        with self._client("dbt.projects.read") as c:
            resp = c.get(f"/api/v1/internal/projects/{project_id}/head")
            resp.raise_for_status()
            return resp.json()["sha"]

    def get_project(self, project_id: str) -> dict[str, Any]:
        with self._client("dbt.projects.read") as c:
            resp = c.get(f"/api/v1/internal/projects/{project_id}")
            resp.raise_for_status()
            return resp.json()

    # ------------------------------------------------------------------
    # Run submission
    # ------------------------------------------------------------------

    def trigger_run(
        self,
        project_id: str,
        *,
        select: str | None = None,
        git_sha: str | None = None,
        idempotency_key: str | None = None,
        scheduled_fire_time: str | None = None,
        trigger: str | None = None,
        triggered_by: str | None = None,
    ) -> dict[str, Any]:
        """Submit a run for project_id. `trigger` is "manual" for a run someone started by hand (`triggered_by` says who); the Runner's
        default is "scheduled" (cron and new commits).

        Returns the run dict (id, status='queued', ...) from the Runner.
        The Runner's RunService handles lease acquisition, k8s Job dispatch,
        and all downstream steps — we just submit and record the run_id.

        With `git_sha` (DG-007) the Runner builds exactly that commit instead of resolving the project's ref
        itself; the run dict's `git_sha` is the commit it froze, for the caller to check.

        With `idempotency_key` (X-004) a retried call returns the run the first call created instead of starting
        a second one; the same key with a different request is refused by the Runner (422).

        With `scheduled_fire_time` (DG-006: the cron instant, timezone-aware ISO-8601) the Runner claims
        (project, instant) atomically with creating the run: a second request for the same instant returns the
        first run instead of starting another, whatever Dagster run it came from.
        """
        body: dict[str, Any] = {}
        if select:
            body["select"] = select
        if git_sha:
            body["git_sha"] = git_sha
        if scheduled_fire_time:
            body["scheduled_fire_time"] = scheduled_fire_time
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        with self._client("dbt.runs.trigger") as c:
            resp = c.post(f"/api/v1/internal/projects/{project_id}/run", json=body, headers=headers,
                          params={k: v for k, v in (("trigger", trigger), ("triggered_by", triggered_by)) if v} or None)
            resp.raise_for_status()
            return resp.json()

    # ------------------------------------------------------------------
    # Run cancellation (DG-017)
    # ------------------------------------------------------------------

    def cancel_run(self, run_id: str, *, wait_seconds: float = 0.0, poll_interval_seconds: float = 1.0) -> dict[str, Any] | None:
        """Ask the Runner to cancel a run (DBT-005: queued -> cancelled; running with a Job -> cancelling, and
        'cancelled' only once the cluster confirms the Job is gone). Returns the run as the Runner reports it, or None
        if there is no such run. A run that already finished (409) is returned as it is, not raised.

        With `wait_seconds` it then polls until the Runner says `cancelled` (or another terminal status), for at most
        that long; a Runner still `cancelling` after that keeps going on its own (its reconciler finishes the job), and
        the returned status says so. Cancelling is idempotent. The request itself is short-timed (it runs while
        Dagster is terminating the step)."""
        with self._client("dbt.runs.cancel", timeout=_CANCEL_TIMEOUT_SECONDS) as c:
            resp = c.post(f"/api/v1/internal/runs/{run_id}/cancel")
        if resp.status_code == 404:
            return None
        if resp.status_code == 409:
            return self.get_run(run_id)
        resp.raise_for_status()
        run = resp.json()
        deadline = time.monotonic() + wait_seconds
        while wait_seconds > 0 and run.get("status") not in _TERMINAL and time.monotonic() < deadline:
            time.sleep(poll_interval_seconds)
            run = self.get_run(run_id)
        return run

    def cancel_run_by_key(
        self, project_id: str, idempotency_key: str, *, attempts: int = 5, retry_delay_seconds: float = 1.0,
    ) -> dict[str, Any] | None:
        """Cancel the run created under `idempotency_key` for a submission whose answer was never seen (the Dagster
        run was terminated while the request was in flight, so there is no run id). The Runner may still be
        processing that request, so a 404 is retried a few times. None if no run ever appeared (or it had already
        finished): nothing to cancel."""
        for attempt in range(1, attempts + 1):
            with self._client("dbt.runs.cancel", timeout=_CANCEL_TIMEOUT_SECONDS) as c:
                resp = c.post(f"/api/v1/internal/projects/{project_id}/runs/cancel", json={"idempotency_key": idempotency_key})
            if resp.status_code == 409:
                return None
            if resp.status_code != 404:
                resp.raise_for_status()
                return resp.json()
            if attempt < attempts:
                time.sleep(retry_delay_seconds)
        return None

    # ------------------------------------------------------------------
    # Run polling
    # ------------------------------------------------------------------

    def get_run(self, run_id: str) -> dict[str, Any]:
        """Fetch the current state of a run from the Runner."""
        with self._client("dbt.runs.read") as c:
            resp = c.get(f"/api/v1/internal/runs/{run_id}")
            resp.raise_for_status()
            return resp.json()

    def poll_run_until_terminal(
        self,
        run_id: str,
        *,
        poll_interval_seconds: float = 10.0,
        max_wait_seconds: float = 7200.0,   # 2h hard ceiling
    ) -> dict[str, Any]:
        """Block until the run reaches a terminal status or the ceiling is hit.

        Terminal statuses: succeeded | failed | cancelled.
        Raises ``RunPollTimeout`` (a ``RuntimeError``) if max_wait_seconds elapses
        without a terminal status. No op polls any more (DG-018: dbt_run_monitor_sensor
        watches submitted runs without holding a Dagster run slot); this stays for
        scripts and ad-hoc use.
        """
        deadline = time.monotonic() + max_wait_seconds
        while True:
            run = self.get_run(run_id)
            status = run.get("status", "unknown")
            log.debug("dbt_runner.poll", run_id=run_id, status=status)
            if status in _TERMINAL:
                return run
            if time.monotonic() >= deadline:
                raise RunPollTimeout(
                    f"Run {run_id} did not reach a terminal status within "
                    f"{max_wait_seconds:.0f} s (last status: {status!r}). "
                    "Check the dbt Runner logs or the Workspace UI for details."
                )
            time.sleep(poll_interval_seconds)

    # ------------------------------------------------------------------
    # Git HEAD SHA (used by git_commit_sensor)
    # ------------------------------------------------------------------

    def get_last_synced_sha(self, project_id: str) -> str | None:
        """Return the SHA the Runner last successfully built, or None."""
        project = self.get_project(project_id)
        return project.get("last_synced_sha")
