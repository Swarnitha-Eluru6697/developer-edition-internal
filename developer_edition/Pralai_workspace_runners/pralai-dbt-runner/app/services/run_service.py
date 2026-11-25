"""Run service. Orchestrates one execution of a project:
clone -> dbt build -> persist node results -> extract lineage -> enqueue
lineage/dataset metadata events -> mark run terminal.

submit() freezes what the run will execute (DBT-006/007: the resolved Git
commit and the DQ schema blocks, see app/services/run_manifest.py), persists a
'queued' row and fires an immediate best-effort
dispatch so a fresh run starts with no perceptible delay — but ownership
of actually running it to completion does not live only in that one
asyncio task. dispatch_run() claims a run with a guarded UPDATE (queued ->
running) that also takes a heartbeat-renewed LEASE on the row and starts a
new attempt (app/services/run_lease.py). Every later step — creating /
releasing the k8s Job, finalizing — is guarded by (owner, attempt) and
status, so a cancel (RunService.cancel's CAS) or a takeover by another
replica after this process stops heartbeating is always respected. See
app/services/run_reconciler.py for the durable half (RUN-DBT-003).

Publishing lineage (to UCS) and dataset registration (to the Datasets
product, TDD 4.4.2) used to be best-effort inline HTTP calls made right
after a run finished — see app.services.metadata_outbox for why that
metadata can no longer simply vanish on a transient outage (RUN-DBT-008).
"""
from __future__ import annotations

import asyncio
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import structlog
import yaml
from fastapi import HTTPException
from sqlalchemy import text, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.git_credentials import token_for
from app.core.log_scrub import scrub_urls
from app.core.errors import not_found
from app.executors.isolation import get_runner_isolation
from app.models import LineageEdge, Project, Run, RunEvent, ScheduledOccurrence
from app.schemas.project import RunTriggerRequest
from app.services import data_dependencies, metadata_outbox, run_lease, run_manifest
from app.services.git_service import get_git_service

log = structlog.get_logger()

# Strong references to in-flight run tasks. Without this, asyncio may
# garbage-collect a task created by create_task before it runs, leaving the
# run stuck in "queued" (never dispatched).
_BG_TASKS: set[asyncio.Task] = set()

# Postgres NOTIFY channel that wakes the leader's reconciler when a run is queued (DBT-011).
RUN_QUEUED_CHANNEL = "dbt_runs_queued"


def _resolve_job_file(clone_dir: Path, file_path: str) -> Path:
    """A SQL or Python job's file inside the clone; refuses a path that leaves the repository or does not exist."""
    root = clone_dir.resolve()
    target = (clone_dir / file_path).resolve()
    if root not in target.parents:
        raise RuntimeError(f"'{file_path}' points outside the repository.")
    if not target.is_file():
        raise RuntimeError(f"File '{file_path}' does not exist in the repository at this commit.")
    return target


def _resolve_project_dir(clone_dir: Path, project_path: str = "") -> Path:
    """Locate the dbt project inside a cloned repo. dbt needs a dbt_project.yml
    at the --project-dir.

    With a project_path (several dbt projects share the repo) that exact folder
    is used. Without one: the repo root if it has a dbt_project.yml, else the
    single dbt_project.yml found in a subfolder; if several exist we can't
    guess, so fail listing them and say how to choose."""
    if project_path:
        root = clone_dir.resolve()
        target = (clone_dir / project_path).resolve()
        # resolve() follows symlinks, so a link pointing out of the clone is caught too.
        if target != root and root not in target.parents:
            raise RuntimeError(f"project_path '{project_path}' points outside the repository.")
        if not target.is_dir():
            raise RuntimeError(f"Folder '{project_path}' does not exist in the repository.")
        if not (target / "dbt_project.yml").is_file():
            raise RuntimeError(f"No dbt_project.yml in folder '{project_path}'.")
        return target
    if (clone_dir / "dbt_project.yml").exists():
        return clone_dir
    matches = sorted(
        (p.parent for p in clone_dir.rglob("dbt_project.yml") if ".git" not in p.parts),
        key=lambda d: len(d.parts),
    )
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise RuntimeError("No dbt_project.yml found in the repository.")
    rels = ", ".join(str(m.relative_to(clone_dir)) for m in matches)
    raise RuntimeError(
        f"Multiple dbt projects found in the repo ({rels}). Register each one with its "
        "folder as the project path (or git_url ending in #subdirectory=<folder>)."
    )


_TEST_NAME_SAFE = re.compile(r"[^A-Za-z0-9_]+")


def _dq_test_named(test, model: str, column: str | None = None):
    """A DQ test with a dbt test name of its own: dq_<rule>_<model>[_<column>]_<first 8 of its check id>. Anything that is not a
    DQ test, or already has a name, is returned unchanged."""
    if not isinstance(test, dict) or len(test) != 1:
        return test
    kind, body = next(iter(test.items()))
    if not isinstance(body, dict) or "dq_rule" not in (body.get("meta") or {}) or body.get("name"):
        return test
    check = str((body.get("meta") or {}).get("dq_check_id") or "")[:8]
    parts = ["dq", str(kind).rsplit(".", 1)[-1], model, column or "", check]
    name = _TEST_NAME_SAFE.sub("_", "_".join(p for p in parts if p)).strip("_").lower()
    return {kind: {"name": name, **body}}


def _test_name(test) -> str:
    """The name dbt gives a test: its explicit `name`, else its type (dbt's default name is built from the type, model and
    column, so two unnamed tests of one type on one column collide)."""
    if isinstance(test, str):
        return test
    if isinstance(test, dict) and len(test) == 1:
        kind, body = next(iter(test.items()))
        if isinstance(body, dict) and body.get("name"):
            return str(body["name"])
        if isinstance(body, dict) and "expression" in body:
            return f"{kind}:{body['expression']}"
        return str(kind)
    return repr(test)


def _apply_dq_schema_tests(blocks: dict[str, str], project_dir: Path) -> None:
    """Merge DQ Service schema.yml blocks into the cloned project.

    dbt refuses to build when a model is described in two places —
    "found two schema.yml entries for the same resource" is a hard Compilation
    Error, not a warning. Writing our own file unconditionally therefore breaks
    every project that already documents its models, which is most of them.

    So: for each rendered model, find the schema.yml that already describes it
    and append our tests to that entry. Only models nobody documents go into the
    standalone generated file. Tests we previously added are tagged with
    `meta.dq_rule`, so a re-run replaces them instead of stacking duplicates.

    Every DQ test gets its own dbt test name (`dq_<rule>_<model>[_<column>]_<check id>`). dbt names a generic test after its
    type, model and column, so a DQ `unique` next to the project's own `unique` would be the same test twice (a hard error);
    they used to be dropped, which meant a ruleset silently did nothing wherever the project already had a test of that kind.
    With a name of its own each DQ check runs alongside the project's tests and is reported as itself.

    Missing or empty blocks are a no-op — a project with no DQ binding must
    still build normally.

    `blocks` are the ones frozen in the run's manifest (DBT-007), never read from the project row at execution time.
    """
    if not blocks:
        return

    settings = get_settings()

    # Parse each rendered block into the model entry it describes.
    rendered: dict[str, dict] = {}
    for table, yaml_text in blocks.items():
        try:
            doc = yaml.safe_load(yaml_text) or {}
        except yaml.YAMLError as e:
            log.warning("dq_schema_tests_unparsable", table=table, error=str(e))
            continue
        for model in doc.get("models") or []:
            if isinstance(model, dict) and model.get("name"):
                model["tests"] = [_dq_test_named(t, model["name"]) for t in model.get("tests") or []]
                for col in model.get("columns") or []:
                    if isinstance(col, dict) and col.get("name"):
                        col["tests"] = [_dq_test_named(t, model["name"], col["name"]) for t in col.get("tests") or []]
                rendered[model["name"]] = model

    if not rendered:
        return

    def _is_ours(test) -> bool:
        """A test this service previously injected, identified by meta.dq_rule."""
        if not isinstance(test, dict):
            return False
        body = next(iter(test.values()), None)
        return isinstance(body, dict) and "dq_rule" in (body.get("meta") or {})

    def _tests_key(entry: dict) -> str:
        """Where this model/column keeps its tests. dbt 1.8+ calls them `data_tests` (`tests` is the old name) and refuses an
        entry that uses both, so ours go under the key the project already uses there; `tests` when it has none."""
        return "data_tests" if "data_tests" in entry and "tests" not in entry else "tests"

    def _merge_into(entry: dict, incoming: list | None) -> None:
        key = _tests_key(entry)
        entry[key] = _merge(entry.get(key), incoming)
        if not entry[key]:
            entry.pop(key)

    def _merge(existing: list | None, incoming: list | None) -> list:
        # The project's own tests stay exactly as they are; ours (previous runs' copies removed) are added after them. Ours carry
        # their own dbt test name, so they never collide with the project's; the same DQ check twice is added once.
        kept = [t for t in (existing or []) if not _is_ours(t)]
        names = {_test_name(t) for t in kept}
        out = list(kept)
        for t in incoming or []:
            name = _test_name(t)
            if name in names:
                continue
            names.add(name)
            out.append(t)
        return out

    merged_into_existing: set[str] = set()

    # Walk every schema file in the project looking for models we render for.
    for path in sorted(project_dir.rglob("*.yml")) + sorted(project_dir.rglob("*.yaml")):
        if path.name == Path(settings.dq_schema_tests_filename).name:
            continue
        try:
            doc = yaml.safe_load(path.read_text()) or {}
        except (yaml.YAMLError, OSError):
            continue
        if not isinstance(doc, dict) or not isinstance(doc.get("models"), list):
            continue

        touched = False
        for model in doc["models"]:
            if not isinstance(model, dict):
                continue
            ours = rendered.get(model.get("name"))
            if not ours:
                continue

            _merge_into(model, ours.get("tests"))

            by_name = {c["name"]: c for c in model.get("columns") or []
                       if isinstance(c, dict) and c.get("name")}
            for col in ours.get("columns") or []:
                target = by_name.get(col["name"])
                if target is None:
                    model.setdefault("columns", []).append(col)
                else:
                    _merge_into(target, col.get("tests"))

            merged_into_existing.add(model["name"])
            touched = True

        if touched:
            path.write_text(yaml.dump(doc, default_flow_style=False, sort_keys=False))
            log.info("dq_schema_tests_merged", path=str(path),
                     models=sorted(m for m in rendered if m in merged_into_existing))

    # Anything the project does not already describe is safe to declare ourselves.
    leftover = [m for name, m in rendered.items() if name not in merged_into_existing]
    tests_path = project_dir / settings.dq_schema_tests_filename
    if leftover:
        tests_path.parent.mkdir(parents=True, exist_ok=True)
        tests_path.write_text(
            "# Generated by inaiera-data-quality — do not edit manually.\n\n"
            + yaml.dump({"version": 2, "models": leftover},
                        default_flow_style=False, sort_keys=False)
        )
        log.info("dq_schema_tests_written", path=str(tests_path),
                 models=[m["name"] for m in leftover])
    elif tests_path.exists():
        # Every model is now documented in-place; a stale generated file would
        # re-introduce the duplicate-resource error it was written to avoid.
        tests_path.unlink()


class RunService:
    def __init__(self, db: AsyncSession):
        self._db = db

    async def submit(
        self, *, project: Project, trigger: str, triggered_by_user_id: str | None,
        request: RunTriggerRequest | None, subject_id: str | None = None, idempotency_key: str | None = None,
    ) -> Run:
        """Freeze and queue a run. `subject_id` is who the manifest says asked for it (the user, or the scheduler
        identity); `idempotency_key` (X-004) makes a retried submission return the run it already created."""
        select_ = request.select if request else None
        pinned = request.git_sha.lower() if request and request.git_sha else None
        fire = request.scheduled_fire_time.astimezone(timezone.utc) if request and request.scheduled_fire_time else None
        if fire is not None:
            _check_occurrence(trigger, fire)
        fingerprint = run_manifest.request_fingerprint(select_, pinned) if idempotency_key else None
        if idempotency_key:
            existing = await self._run_by_key(project.id, idempotency_key)
            if existing is not None:
                return _replay(existing, fingerprint)
        occurrence = None
        if fire is not None:
            # DG-006: claim (project, instant) BEFORE doing anything else. The unique constraint makes this atomic across
            # replicas and retries; the row lives in this transaction, so a submission that fails below leaves no claim.
            occurrence, earlier = await self._claim_occurrence(project.id, fire)
            if earlier is not None:
                return _replay_occurrence(earlier, select_, pinned)
        try:
            manifest = await freeze_project(
                self._db, project.id, select_, git_sha=pinned, subject_id=subject_id or triggered_by_user_id
                or "scheduler", trigger=trigger, idempotency_key=idempotency_key)
        except run_manifest.GitRefError as e:
            raise _git_error(e) from None
        run = Run(
            project_id=project.id,
            trigger=trigger,
            triggered_by_user_id=triggered_by_user_id,
            select=select_,
            status="queued",
            git_sha=manifest["git"]["sha"],
            manifest=manifest,
            manifest_sha256=run_manifest.manifest_sha256(manifest),
            idempotency_key=idempotency_key,
            idempotency_request_hash=fingerprint,
        )
        try:
            async with self._db.begin_nested():
                self._db.add(run)
                await self._db.flush()
        except IntegrityError:
            # A concurrent submission with the same key won the unique index: return its run.
            existing = await self._run_by_key(project.id, idempotency_key) if idempotency_key else None
            if existing is None:
                raise
            return _replay(existing, fingerprint)
        if occurrence is not None:
            occurrence.run_id = run.id
            await self._db.flush()
        run_id = run.id

        # DBT-011: submit PERSISTS the queued run and nothing else. It does not start execution in this request's process: the one durable
        # owner of dispatch is the leader's reconciler loop (app/services/run_reconciler.py), which claims a queued run only after this
        # transaction has committed. The NOTIFY below is delivered at commit and just wakes that loop for low latency; if it is lost the
        # loop's own short poll finds the row anyway, so there is no second ownership path to reason about (no in-process task, no
        # race with the commit, nothing lost if this pod dies right after responding).
        await self._db.execute(text("SELECT pg_notify(:channel, :run_id)"),
                               {"channel": RUN_QUEUED_CHANNEL, "run_id": str(run_id)})
        return run

    async def _claim_occurrence(self, project_id: uuid.UUID, fire: datetime) -> tuple[ScheduledOccurrence | None, Run | None]:
        """(the new occurrence, None) if this call won the claim; (None, the run it created) if someone already had.
        A concurrent claimer waits on the unique index until the winner commits or rolls back."""
        occurrence = ScheduledOccurrence(project_id=project_id, scheduled_fire_time=fire)
        try:
            async with self._db.begin_nested():
                self._db.add(occurrence)
                await self._db.flush()
        except IntegrityError:
            earlier = (await self._db.execute(
                select(ScheduledOccurrence).where(ScheduledOccurrence.project_id == project_id,
                                                  ScheduledOccurrence.scheduled_fire_time == fire)
            )).scalar_one()
            run = await self._db.get(Run, earlier.run_id) if earlier.run_id else None
            if run is None:
                # The run was purged by retention: the occurrence DID fire, and must not fire again.
                raise HTTPException(status_code=409, detail={
                    "code": "OCCURRENCE_ALREADY_FIRED",
                    "message": f"the occurrence {fire.isoformat()} was already fired and its run no longer exists"}) from None
            log.info("occurrence_replayed", project_id=str(project_id), scheduled_fire_time=fire.isoformat(),
                     run_id=str(run.id))
            return None, run
        return occurrence, None

    async def _run_by_key(self, project_id: uuid.UUID, key: str) -> Run | None:
        return (await self._db.execute(
            select(Run).where(Run.project_id == project_id, Run.idempotency_key == key)
        )).scalar_one_or_none()

    async def get(self, project: Project, run_id: uuid.UUID) -> Run:
        run = await self._db.get(Run, run_id)
        if not run or run.project_id != project.id:
            raise not_found("run", str(run_id))
        return run

    async def list_for_project(self, project: Project, limit: int = 50) -> list[Run]:
        settings = get_settings()
        result = await self._db.execute(
            select(Run)
            .where(Run.project_id == project.id)
            .order_by(Run.created_at.desc())
            .limit(min(limit, settings.max_run_history_per_project))
        )
        return list(result.scalars())

    async def list_events(self, run: Run) -> list[RunEvent]:
        result = await self._db.execute(
            select(RunEvent).where(RunEvent.run_id == run.id).order_by(RunEvent.created_at)
        )
        return list(result.scalars())

    async def list_lineage(self, project: Project) -> list[LineageEdge]:
        result = await self._db.execute(
            select(LineageEdge).where(LineageEdge.project_id == project.id)
        )
        return list(result.scalars())

    async def cancel(self, project: Project, run_id: uuid.UUID, *, isolation=None) -> Run:
        """Ask for a queued or running run to be cancelled (DBT-005: desired state, see app/services/run_cancel.py).

        One compare-and-set decides the transition, so two concurrent cancels, or a cancel racing a
        dispatch/finalize, can never both "win": the dispatcher's own guarded steps (record Job name, release
        Job, finalize) all require status='running' and fail once this commits, so a cancelled run never
        starts and is never overwritten.

          * queued                                  -> cancelled (nothing can be running)
          * running, no Job recorded (or not k8s)   -> cancelled (the Job name is recorded BEFORE the Job is
                                                       created, so no name means no Job)
          * running, Job recorded                   -> cancelling; the Job's deletion is requested at once and
                                                       'cancelled' is written only after the cluster confirms it
                                                       is gone (here if that is already true, else the reconciler)

        Cancelling a run that is already 'cancelling' is idempotent (and nudges the deletion again); a finished
        run is a 409."""
        from sqlalchemy import String, case, literal, or_

        from app.core.errors import conflict
        from app.services import run_cancel

        project_id = project.id
        k8s = get_settings().runner_mode == "k8s_job"
        immediate = or_(Run.status == "queued", Run.k8s_job_name.is_(None), literal(not k8s))
        result = await self._db.execute(
            update(Run)
            .where(Run.id == run_id, Run.project_id == project_id, Run.status.in_(("queued", "running")))
            .values(status=case((immediate, literal("cancelled", String)), else_=literal("cancelling", String)),
                    completed_at=case((immediate, func.now()), else_=None),
                    cancel_requested_at=func.now(), lease_owner=None, lease_expires_at=None)
            .returning(Run.status)
        )
        row = result.one_or_none()
        if row is None:
            await self._db.rollback()
            current = (await self._db.execute(
                select(Run.status).where(Run.id == run_id, Run.project_id == project_id)
            )).scalar_one_or_none()
            if current is None:
                raise not_found("run", str(run_id))
            if current != "cancelling":
                raise conflict(f"run is already {current}; cannot cancel", code="RUN_NOT_CANCELLABLE", resource="run", field="status")
        else:
            await self._db.commit()
        # 'cancelling' (just now, or already): ask the cluster once for an immediate answer; the reconciler
        # keeps going if it is not confirmed yet.
        if row is None or row[0] == "cancelling":
            await run_cancel.advance(run_id, isolation or get_runner_isolation())

        run = (await self._db.execute(
            select(Run).where(Run.id == run_id).execution_options(populate_existing=True)
        )).scalar_one()
        if run.status == "cancelled" and run.started_at and run.completed_at and run.duration_ms is None:
            run.duration_ms = int((run.completed_at - run.started_at).total_seconds() * 1000)
            await self._db.commit()
        return run


_OCCURRENCE_FUTURE_SKEW = timedelta(minutes=5)


def _check_occurrence(trigger: str, fire: datetime) -> None:
    if trigger != "scheduled":
        raise HTTPException(status_code=422, detail={
            "code": "OCCURRENCE_NOT_SCHEDULED", "message": "scheduled_fire_time is only accepted for scheduled runs"})
    if fire > datetime.now(timezone.utc) + _OCCURRENCE_FUTURE_SKEW:
        raise HTTPException(status_code=422, detail={
            "code": "OCCURRENCE_IN_THE_FUTURE",
            "message": f"scheduled_fire_time {fire.isoformat()} has not happened yet"})


def _replay_occurrence(existing: Run, select_: str | None, pinned: str | None) -> Run:
    """The run an occurrence already created, unless this request asks for something different from it."""
    if (existing.select or None) != (select_ or None) or (pinned and existing.git_sha != pinned):
        raise HTTPException(status_code=422, detail={
            "code": "OCCURRENCE_REUSED",
            "message": "this scheduled occurrence already has a run for a different request"})
    return existing


def _replay(existing: Run, fingerprint: str | None) -> Run:
    """A retried submission: the run it created, unless the key was first used for a different request."""
    if existing.idempotency_request_hash not in (None, fingerprint):
        raise HTTPException(status_code=422, detail={
            "code": "IDEMPOTENCY_KEY_REUSED",
            "message": "this Idempotency-Key was already used with a different request"})
    return existing


def _git_error(e: run_manifest.GitRefError) -> HTTPException:
    return HTTPException(status_code=422 if e.code == "GIT_REF_NOT_FOUND" else 502,
                         detail={"code": e.code, "message": e.message})


async def freeze_project(db: AsyncSession, project_id: uuid.UUID, select_: str | None,
                         git_sha: str | None = None, *, subject_id: str | None = None,
                         trigger: str | None = None, idempotency_key: str | None = None) -> dict:
    """Build the execution manifest for a run of `project_id` as the project is right now (DBT-006/007). Reads the
    project row fresh (the caller's object may be stale), resolves its Git ref to a commit and snapshots its DQ
    blocks. Raises GitRefError if the ref cannot be resolved: a run that would have to guess its source at dispatch
    time is never queued (submit turns this into a 422/502).

    With `git_sha` (DG-007: the commit Dagster's git sensor saw) that exact commit is frozen instead and the ref is
    not consulted, so the caller's label and what executes cannot differ, even if the branch has moved on."""
    row = (await db.execute(
        select(Project.git_url, Project.git_ref, Project.engine, Project.project_path,
               Project.dq_schema_tests, Project.dq_schema_tests_meta, Project.org_id, Project.workspace_id,
               Project.credential_ref, Project.kind, Project.connection_id)
        .where(Project.id == project_id)
    )).one_or_none()
    if row is None or row.org_id != get_settings().org_id:                              # DBT-004
        raise not_found("project", str(project_id))
    if git_sha:
        sha, source = git_sha.lower(), "request"
    else:
        try:
            sha, source = await run_manifest.resolve_commit(row.git_url, row.git_ref, token_for(row.credential_ref)), "ref"
        except run_manifest.GitRefError as e:
            log.warning("run_ref_unresolvable", project_id=str(project_id), code=e.code, error=e.message)
            raise
    return run_manifest.build_manifest(
        git_url=row.git_url, git_ref=row.git_ref, git_sha=sha, project_path=row.project_path or "",
        engine=row.engine, select=select_, dq_blocks=row.dq_schema_tests if row.kind == "dbt" else None,
        dq_meta=row.dq_schema_tests_meta if row.kind == "dbt" else None,
        git_source=source, org_id=row.org_id, workspace_id=str(row.workspace_id) if row.workspace_id else None,
        subject_id=subject_id, trigger=trigger, idempotency_key=idempotency_key, kind=row.kind or "dbt",
        connection_id=str(row.connection_id) if row.connection_id else None,
    )


async def _fail_queued(run_id: uuid.UUID, message: str) -> None:
    """Fail a run that never started (guarded: only while still queued)."""
    async with _session() as db:
        await db.execute(
            update(Run).where(Run.id == run_id, Run.status == "queued")
            .values(status="failed", completed_at=func.now(), error_message=scrub_urls(message)[:1000])
        )
        await db.commit()
    log.error("run_failed_before_start", run_id=str(run_id), reason=message)


async def _freeze_legacy_run(run_id: uuid.UUID, select_: str | None, project_id: uuid.UUID) -> dict | None:
    """A queued run with no manifest was submitted by an older runner version (a rolling deploy). Freeze it now,
    BEFORE it becomes executable, with the same one-time guarded write the trigger allows. None if it cannot be
    frozen (the run is then failed, or someone else froze it first and we read theirs)."""
    async with _session() as db:
        try:
            manifest = await freeze_project(db, project_id, select_)
        except run_manifest.GitRefError as e:
            await _fail_queued(run_id, f"could not freeze the run's Git commit: {e.message}")
            return None
        claimed = await db.execute(
            update(Run).where(Run.id == run_id, Run.status == "queued", Run.manifest.is_(None))
            .values(manifest=manifest, manifest_sha256=run_manifest.manifest_sha256(manifest),
                    git_sha=manifest["git"]["sha"])
        )
        await db.commit()
        if claimed.rowcount == 1:
            log.warning("run_manifest_frozen_late", run_id=str(run_id), sha=manifest["git"]["sha"],
                        note="run was queued by an older runner version")
            return manifest
        existing = (await db.execute(select(Run.manifest, Run.manifest_sha256).where(Run.id == run_id))).one_or_none()
    if existing and existing.manifest:
        return run_manifest.verify(existing.manifest, existing.manifest_sha256)
    return None


async def _claim_run(run_id: uuid.UUID) -> int | None:
    """Atomically transition a run from 'queued' to 'running', taking its
    lease and starting a new attempt. Returns the attempt number, or None
    if someone else (an earlier attempt, another replica, the reconciler)
    already claimed it or it was cancelled — dispatch_run() treats that as
    a no-op, which is what makes it safe to call repeatedly."""
    return await run_lease.claim_queued(run_id)


def _session():
    from app.db import session as db_session  # resolved at call time (tests patch it)
    return db_session.AsyncSessionLocal()


async def dispatch_run(run_id: uuid.UUID) -> None:
    """Claim and fully execute one run. Idempotent: if the claim fails
    (run is not 'queued' any more) this returns immediately without
    touching anything. Called ONLY by run_reconciler's queued sweep on the
    elected leader (DBT-011) — request handlers persist a queued run and
    never start execution themselves.

    What it executes comes from the run's manifest (DBT-006/007), never from the project's current Git ref or DQ
    blocks, so it is the same on the first attempt and on every retry."""
    async with _session() as db:
        # The request that created this run commits its session only after
        # its handler returns (see get_db), so the row may not be visible to
        # this fresh session yet. Wait briefly for it.
        run = None
        for _ in range(100):  # up to ~10s
            run = await db.get(Run, run_id)
            if run:
                break
            await asyncio.sleep(0.1)
            db.expire_all()
        if not run:
            log.error("run_row_never_visible", run_id=str(run_id))
            return
        project = await db.get(Project, run.project_id)
        if not project:
            log.error("run_project_missing", run_id=str(run_id), project_id=str(run.project_id))
            return
        if project.org_id != get_settings().org_id:                                    # DBT-004: never execute another org's run
            log.error("run_project_other_org", run_id=str(run_id), project_id=str(project.id))
            return
        project_id, git_url, select_ = project.id, project.git_url, run.select
        credential_ref = project.credential_ref
        manifest, manifest_hash, status = run.manifest, run.manifest_sha256, run.status

    if status != "queued":
        return
    if manifest is None:
        manifest = await _freeze_legacy_run(run_id, select_, project_id)
        if manifest is None:
            return
    else:
        try:
            run_manifest.verify(manifest, manifest_hash)
        except run_manifest.ManifestIntegrityError as e:
            await _fail_queued(run_id, f"refusing to run: {e}")
            return
    if run_manifest.redact_url(git_url) != manifest["git"]["url"]:
        await _fail_queued(run_id, "refusing to run: the project's repository is not the one this run was "
                                   "submitted for")
        return

    attempt = await _claim_run(run_id)
    if attempt is None:
        return

    await _execute_claimed_run(
        run_id=run_id, project_id=project_id, git_url=git_url, manifest=manifest, attempt=attempt,
        credential_ref=credential_ref,
    )


async def _execute_claimed_run(
    *, run_id: uuid.UUID, project_id: uuid.UUID, git_url: str, manifest: dict, attempt: int,
    credential_ref: str | None = None,
) -> None:
    """Runs an already-claimed ('running', leased to this process) run to a
    terminal state, renewing its lease throughout. Every externally visible
    step re-checks ownership, and finalization is guarded by (owner,
    attempt), so a cancel or a takeover is always respected."""
    settings = get_settings()
    isolation = get_runner_isolation()
    sha: str | None = None
    tree_sha: str | None = None
    k8s_mode = settings.runner_mode == "k8s_job"
    git_svc = None if k8s_mode else get_git_service()
    git = manifest["git"]
    project_path = manifest["project_path"]
    kind = run_manifest.job_kind(manifest)
    dq_blocks = run_manifest.dq_blocks(manifest)
    git_token = token_for(credential_ref)         # DBT-009: resolved here, handed to git through its environment, never the URL

    async with run_lease.RunLease(run_id, attempt) as lease:
        try:
            project_dir = None
            if not k8s_mode:
                assert git_svc is not None      # set above whenever runner_mode != "k8s_job"
                # Subprocess (dev) mode builds in this process: clone here.
                # In k8s_job mode the isolated Job clones; the API never
                # fetches customer code itself (the Job reports git_sha).
                clone_dir = git_svc.clone_for_run(git_url=git_url, git_ref=git["ref"], run_id=run_id,
                                                  sha=git["sha"], token=git_token)
                sha = git_svc.head_sha(clone_dir)
                if kind in ("sql", "python"):
                    project_dir = _resolve_job_file(clone_dir, project_path)
                    tree_sha = git_svc.tree_sha(clone_dir, project_dir)     # the file's own Git hash (X-004 evidence)
                else:
                    project_dir = _resolve_project_dir(clone_dir, project_path)
                    tree_sha = git_svc.tree_sha(clone_dir, project_dir)   # before DQ rules touch the working tree
                    _apply_dq_schema_tests(dq_blocks, project_dir)

            result = await isolation.execute_run(
                run_id=run_id, project_dir=project_dir, engine=manifest["engine"], select=manifest["select"],
                git_url=git_url, git_ref=git["ref"], project_id=project_id, lease=lease,
                project_path=project_path, git_sha=git["sha"], dq_schema_blocks=dq_blocks, git_token=git_token,
                kind=kind, connection_id=run_manifest.job_connection(manifest),
            )
            if tree_sha:
                result["source_tree_sha"] = tree_sha
        except Exception as e:
            log.exception("run_failed", run_id=str(run_id))
            result = {"success": False, "errors": [scrub_urls(str(e))], "nodes": []}
        finally:
            if git_svc is not None:
                git_svc.cleanup(run_id)

        await _complete_attempt(run_id, project_id, attempt, sha, result, owner=lease.owner)


async def _complete_attempt(run_id: uuid.UUID, project_id: uuid.UUID, attempt: int,
                            sha: str | None, result: dict, *, owner: str) -> None:
    """Common tail for an attempt's outcome, used by dispatch and by the
    reconciler's adoption path."""
    if result.get("_aborted"):
        log.info("run_attempt_aborted", run_id=str(run_id), attempt=attempt,
                 reason=(result.get("errors") or [None])[0])
        return
    if result.get("_never_started"):
        if await run_lease.requeue(run_id, attempt, owner):
            log.warning("run_requeued_never_started", run_id=str(run_id), attempt=attempt)
        return
    k8s_job = result.pop("_k8s_job", None)
    finalized = await _finalize_run(run_id, project_id, sha or result.get("git_sha"), result,
                                    attempt=attempt, owner=owner)
    if k8s_job and finalized:
        # Only now that the outcome is durable may the Job (and its logs) go.
        try:
            await get_runner_isolation().cleanup_job(run_id=run_id, attempt=attempt,
                                                     job_name=k8s_job["name"])
        except Exception:  # noqa: BLE001 — TTL is the backstop
            log.warning("k8s_job_cleanup_failed", run_id=str(run_id), exc_info=True)


async def _finalize_run(
    run_id: uuid.UUID, project_id: uuid.UUID, sha: str | None, result: dict,
    *, attempt: int | None = None, owner: str | None = None,
) -> bool:
    """Persist a run's terminal outcome. Guarded: only a run still
    'running' — and, when `owner`/`attempt` are given (every production
    caller), still leased to that owner for that attempt — is finalized.
    Anything else (cancelled, already finalized, taken over) is a no-op
    returning False, and callers must not repeat side effects."""
    settings = get_settings()
    conditions = [Run.id == run_id, Run.status == "running"]
    if owner is not None:
        conditions.append(Run.lease_owner == owner)
    if attempt is not None:
        conditions.append(Run.attempt == attempt)

    async with _session() as db:
        # DBT-006: the run's commit was frozen at submission. A build that reports a different one did not execute
        # the source the run was submitted for, so it cannot be recorded as a success.
        frozen = (await db.execute(select(Run.git_sha).where(Run.id == run_id))).scalar_one_or_none()
        if frozen and sha and sha != frozen:
            result = {"success": False, "nodes": [], "_artifact_uri": result.get("_artifact_uri"),
                      "errors": [f"the build executed commit {sha} but the run was submitted for {frozen}"]}
        claim = await db.execute(
            update(Run)
            .where(*conditions)
            .values(status="succeeded" if result["success"] else "failed",
                    lease_owner=None, lease_expires_at=None,
                    result_ref=result.get("_artifact_uri"))
        )
        if claim.rowcount != 1:
            await db.rollback()
            return False

        run = await db.get(Run, run_id)
        if sha and not run.git_sha:     # a run queued by an older runner has no frozen commit
            run.git_sha = sha
        if result.get("source_tree_sha"):
            run.source_tree_sha = str(result["source_tree_sha"])[:64]
        run.completed_at = datetime.now(timezone.utc)
        if run.started_at:
            run.duration_ms = int((run.completed_at - run.started_at).total_seconds() * 1000)
        nodes = result.get("nodes", [])
        run.models_total = len(nodes)
        run.models_succeeded = sum(1 for n in nodes if n["status"] in ("success", "pass"))
        run.models_failed = sum(1 for n in nodes if n["status"] in ("error", "fail"))
        if not result["success"]:
            run.error_message = scrub_urls("; ".join(result.get("errors", [])))[:1000]

        if result["success"]:                                           # a SQL job: the tables its statements read and wrote
            for source, target in result.get("lineage") or []:
                db.add(LineageEdge(run_id=run_id, project_id=project_id, source_table=source, target_table=target, relationship_type="sql"))

        for n in nodes:
            db.add(RunEvent(
                run_id=run_id, event_type="node_finish",
                node_id=n.get("node_id"), payload=n,
            ))

        manifest = result.get("manifest")
        if result["success"] and manifest:
            edges = _extract_lineage(manifest)
            for src, tgt, rel in edges:
                db.add(LineageEdge(
                    run_id=run_id, project_id=project_id,
                    source_table=src, target_table=tgt, relationship_type=rel,
                ))

        # RUN-DBT-008: queued in the SAME transaction as the run's terminal
        # state. A sink that is not configured gets nothing queued at all —
        # rows for a disabled sink would otherwise pile up forever.
        lineage_event = result.get("lineage_event")
        if lineage_event:
            if settings.openlineage_sink_enabled:
                await metadata_outbox.enqueue(
                    db, run_id=run_id, project_id=project_id,
                    event_type="openlineage", payload=lineage_event,
                )
            else:
                log.info("metadata_sink_disabled_skip_enqueue", sink="openlineage", run_id=str(run_id))
        dataset_payload = _build_dataset_registration_payload(project_id, run_id, manifest) \
            if result["success"] and manifest else None
        if dataset_payload:
            if settings.datasets_sink_enabled:
                await metadata_outbox.enqueue(
                    db, run_id=run_id, project_id=project_id,
                    event_type="dataset_registration", payload=dataset_payload,
                )
            else:
                log.info("metadata_sink_disabled_skip_enqueue", sink="datasets", run_id=str(run_id))

        await db.commit()

    return True


def _extract_lineage(manifest: dict) -> list[tuple[str, str, str]]:
    """Walk dbt's manifest.json child_map / depends_on to produce
    (source_table, target_table, relationship_type) edges for models.

    Plus "sql" edges: tables a model's compiled SQL names directly (after FROM / JOIN, schema-qualified) that are neither built by this
    project nor already a ref/source of the model — how a project reading another project's table without a dbt source shows up
    (app/services/data_dependencies.py)."""
    edges: list[tuple[str, str, str]] = []
    nodes = manifest.get("nodes", {})
    sources = manifest.get("sources", {})
    built = {data_dependencies.short_name(n["relation_name"]) for n in nodes.values()
             if n.get("relation_name") and n.get("resource_type") in ("model", "seed", "snapshot")}
    for unique_id, node in nodes.items():
        if node.get("resource_type") != "model":
            continue
        target = node.get("relation_name") or node.get("name")
        known = set(built)
        for dep_id in node.get("depends_on", {}).get("nodes", []):
            dep = nodes.get(dep_id) or sources.get(dep_id)
            if not dep:
                continue
            source = dep.get("relation_name") or dep.get("name")
            rel = "source" if dep_id in sources else "ref"
            edges.append((source, target, rel))
            known.add(data_dependencies.short_name(source))
        for table in data_dependencies.table_refs(node.get("compiled_code") or ""):
            if data_dependencies.short_name(table) not in known:
                edges.append((table, target, "sql"))
                known.add(data_dependencies.short_name(table))
    return edges


def _build_dataset_registration_payload(
    project_id: uuid.UUID, run_id: uuid.UUID, manifest: dict,
) -> dict | None:
    """TDD 4.4.2: the resulting dataset(s) from a successful build,
    shaped for POST /v1/datasets/register-batch. Pure — building the
    payload from the manifest is separate from actually delivering it
    (app.services.datasets_client, via the durable outbox — RUN-DBT-008),
    the same split as openlineage_emitter's build/deliver."""
    nodes = manifest.get("nodes", {})
    datasets = [
        {
            "name": n.get("name"),
            "relation_name": n.get("relation_name"),
            "project_id": str(project_id),
            "run_id": str(run_id),
        }
        for n in nodes.values() if n.get("resource_type") == "model"
    ]
    return {"datasets": datasets} if datasets else None
