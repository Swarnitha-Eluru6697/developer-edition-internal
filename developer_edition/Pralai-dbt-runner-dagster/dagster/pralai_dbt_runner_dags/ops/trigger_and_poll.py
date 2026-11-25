"""Dagster op that submits a dbt run to the Runner (DG-018: it does not wait for it).

``submit_run_op`` POSTs /api/v1/internal/projects/{id}/run, records the returned run id in Dagster's metadata and tags
its own Dagster run so ``dbt_run_monitor_sensor`` reports the outcome later (see monitoring.py). The Dagster run then
ends, instead of holding a run slot, and a worker process, for as long as the Kubernetes Job takes.
"""

import structlog
from dagster import (
    Config,
    Failure,
    DagsterExecutionInterruptedError,
    MetadataValue,
    OpExecutionContext,
    op,
)

from pralai_dbt_runner_dags import dependencies, monitoring
from pralai_dbt_runner_dags.resources.dbt_runner_client import DbtRunnerClient

log = structlog.get_logger()

# DG-017: how long a step that is being stopped waits for the Runner to confirm `cancelled` (the Runner finishes a
# cancellation that takes longer on its own). Short: Dagster is terminating the step while this runs.
CANCEL_CONFIRM_SECONDS = 10.0


def _interrupted(exc: BaseException) -> bool:
    """Dagster (or the operator's Ctrl-C) is stopping this step: not an ordinary Exception."""
    return isinstance(exc, DagsterExecutionInterruptedError) or not isinstance(exc, Exception)


def _cancel_quietly(client: DbtRunnerClient, run_id: str, reason: str) -> None:
    """DG-017: stop the Runner's run because Dagster no longer stands behind it. Best effort: it never raises, so it can
    never hide the error or interrupt that brought us here."""
    try:
        run = client.cancel_run(run_id, wait_seconds=CANCEL_CONFIRM_SECONDS)
        log.warning("dagster.run_cancelled_downstream", run_id=run_id, reason=reason,
                    runner_status=(run or {}).get("status", "unknown"))
    except Exception as e:  # noqa: BLE001 — the original error is the one that matters
        log.error("dagster.cancel_downstream_failed", run_id=run_id, reason=reason, error=str(e))


class SubmitRunConfig(Config):
    project_id: str
    select: str = ""  # empty string means full build
    # DG-006: the cron instant (timezone-aware ISO-8601) the project_schedule_sensor is firing. Sent to the Runner, which
    # claims (project, instant) with the run so one occurrence never starts two dbt runs. Empty for every other trigger.
    scheduled_fire_time: str = ""
    # DG-007: the full commit SHA the Runner must build (the git sensor sets it to the commit it saw). Empty means
    # "the project's ref as of submission" (cron and manual runs), which the Runner then freezes itself.
    git_sha: str = ""


@op(required_resource_keys={"dbt_runner"})
def submit_run_op(context: OpExecutionContext, config: SubmitRunConfig) -> str:
    """POST /api/v1/internal/projects/{id}/run and record the run_id.

    Returns the dbt Runner run_id (UUID string). It does not wait for the run: the Dagster run ends here and
    dbt_run_monitor_sensor reports the outcome (DG-018).
    """
    client: DbtRunnerClient = context.resources.dbt_runner
    select = config.select.strip() or None
    git_sha = config.git_sha.strip().lower() or None
    scheduled_fire_time = config.scheduled_fire_time.strip() or None
    project_id = config.project_id

    log.info(
        "dagster.submit_run",
        project_id=config.project_id,
        select=select,
        git_sha=git_sha,
        scheduled_fire_time=config.scheduled_fire_time,
    )

    # Dependencies: nothing is sent to the Runner unless every job above this one has a recent success (dependencies.py). This is the
    # one door every Dagster run of the project goes through: cron, new commit, the Dagster UI and the Workspace's Run now.
    try:
        dependencies.enforce(context.instance, client, project_id)
    except dependencies.DependencyBlocked as blocked:
        raise Failure(description=blocked.reason, metadata={
            "project_id": MetadataValue.text(project_id),
            "blocked_by": MetadataValue.json([{"project": b.name, "reason": b.reason} for b in blocked.blockers]),
        }) from None

    # X-004: a step retry (or a re-delivered launch) of THIS Dagster run must not start a second dbt run: the Runner
    # returns the run it already created for this key. A new Dagster run has a new run id, so it is a new dbt run.
    idempotency_key = f"dagster:{context.run_id}:submit_run_op"
    # How the Runner labels the run. A cron fire (scheduled_fire_time) or a new commit (git_sha) is "scheduled", the Runner's default. Anything
    # else is somebody starting the job by hand (the Workspace's Run now, or a launch in Dagster's UI): "manual", with who when known (Run now
    # tags the Dagster run with the user).
    this_run = context.instance.get_run_by_id(context.run_id)             # None when the op is invoked directly (no run exists)
    who = ((this_run.tags or {}).get(monitoring.TAG_TRIGGERED_BY) if this_run is not None else None) or None
    by_hand = {} if (scheduled_fire_time or git_sha) else {"trigger": "manual", **({"triggered_by": who} if who else {})}
    try:
        run_data = client.trigger_run(project_id, select=select, git_sha=git_sha,
                                      idempotency_key=idempotency_key, scheduled_fire_time=scheduled_fire_time, **by_hand)
    except BaseException as exc:
        if _interrupted(exc):
            # DG-017: stopped while the request was in flight. The Runner may have processed it although we never saw
            # the answer, so there is no run id: find the run by the key we sent and cancel it.
            try:
                client.cancel_run_by_key(project_id, idempotency_key)
            except Exception as e:  # noqa: BLE001
                log.error("dagster.cancel_downstream_failed", project_id=project_id, key=idempotency_key, error=str(e))
        raise
    run_id: str = run_data["id"]

    if git_sha:
        # The Runner freezes the commit it will build at submission and reports it back. If it is not the one we
        # asked for (or the Runner is too old to know `git_sha` and silently used the branch tip), this Dagster run
        # is labelled with a commit the Runner is not building: stop here rather than report a false provenance.
        frozen = (run_data.get("git_sha") or "").lower()
        if not frozen:
            _cancel_quietly(client, run_id, "the run does not report its commit")
            raise RuntimeError(
                f"dbt Runner run {run_id} does not report the commit it will build, so it cannot be confirmed to "
                f"be the requested commit {git_sha}. The Runner is probably too old to accept `git_sha`; upgrade it.")
        if frozen != git_sha:
            _cancel_quietly(client, run_id, "the run froze a different commit")   # it must not go on to build it
            raise RuntimeError(
                f"dbt Runner run {run_id} was frozen at commit {frozen}, not the requested commit {git_sha}.")

    # DG-018: hand the outcome to dbt_run_monitor_sensor. The tags ARE the hand-over, so if they cannot be written the run
    # must not be left building unobserved: cancel it and fail this step.
    try:
        context.instance.add_run_tags(context.run_id, {
            monitoring.TAG_MONITOR: monitoring.PENDING, monitoring.TAG_RUNNER_RUN: run_id, "project_id": project_id})
    except Exception as e:
        _cancel_quietly(client, run_id, "the run could not be registered for monitoring")
        raise RuntimeError(f"could not register dbt Runner run {run_id} for monitoring ({e}); it was cancelled") from e

    context.add_output_metadata(
        {
            "run_id": MetadataValue.text(run_id),
            "project_id": MetadataValue.text(config.project_id),
            "status": MetadataValue.text(run_data.get("status", "queued")),
            "scheduled_fire_time": MetadataValue.text(config.scheduled_fire_time or "n/a"),
            "select": MetadataValue.text(select or "(full build)"),
            "git_sha": MetadataValue.text(str(run_data.get("git_sha") or "")),
            "runner_api_url": MetadataValue.url(  # the Runner's internal API (needs its token), not a UI page
                f"{client.base_url}/api/v1/internal/runs/{run_id}"
            ),
        }
    )

    log.info("dagster.run_submitted", run_id=run_id, project_id=config.project_id)
    return run_id
