"""Entrypoint for the per-run Kubernetes Job that executes customer dbt
code in production (RUNNER_MODE=k8s_job).

Invoked as `python -m app.executors.job_entrypoint` by the Job pod that
RunnerIsolation submits — see app/executors/isolation.py. It runs in the
same image as the API (or the Dockerfile's slimmer `job` target), never
inside the API process.

It intentionally does NOT touch the metadata database: the Job pod gets
only what it needs to run dbt (a Git ref to clone, the already-rendered DQ
schema-test blocks, engine credentials and artifact-store credentials via
secretKeyRef), nothing else.

Result transport (RUN-DBT-002c): the full result (manifest included) is
written to the durable artifact store under a key derived from (run_id,
attempt), and only a small reference is printed:

    ===DBTRUNNER_RESULT_REF=== {"uri": "s3://…/result.json", "sha256": "…", "success": true}

If the store is unavailable, a SIZE-CAPPED inline fallback is printed
between the legacy markers instead (manifest/lineage dropped and node list
truncated as needed to stay under runner_inline_result_max_bytes, flagged
with "_truncated": true):

    ===DBTRUNNER_RESULT_START===
    {...}
    ===DBTRUNNER_RESULT_END===

Exit code 0 means "ran to completion and reported a result" — including a
*failed* dbt build. A nonzero exit (crash before reporting) makes
Kubernetes mark the Job Failed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid

import structlog

from app.core import log_scrub
from app.executors.dbt_executor import get_dbt_executor
from app.services.git_service import get_git_service

log = structlog.get_logger()
log_scrub.install()          # DG-011: the Job pod's logs go through the same scrubber

RESULT_START = "===DBTRUNNER_RESULT_START==="
RESULT_END = "===DBTRUNNER_RESULT_END==="
RESULT_REF = "===DBTRUNNER_RESULT_REF==="


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="dbt Runner isolated Job entrypoint")
    parser.add_argument("--run-id", required=True, type=uuid.UUID)
    parser.add_argument("--attempt", type=int, default=1)
    parser.add_argument("--engine", required=True)
    parser.add_argument("--select", default=None)
    # "dbt" (a dbt project folder) or "sql" (RUNNER_PROJECT_PATH is one .sql file, run by app/executors/sql_job_executor.py).
    parser.add_argument("--kind", choices=("dbt", "sql", "python"), default="dbt")
    return parser.parse_args(argv)


def size_capped_inline(result: dict, max_bytes: int) -> str:
    """Serialize `result` to at most max_bytes, shedding the bulky parts in
    order of least importance. Always yields valid JSON with the outcome."""
    payload = json.dumps(result, default=str)
    if len(payload.encode()) <= max_bytes:
        return payload
    slim = {k: v for k, v in result.items() if k not in ("manifest", "lineage_event")}
    slim["_truncated"] = True
    payload = json.dumps(slim, default=str)
    nodes = list(slim.get("nodes") or [])
    while len(payload.encode()) > max_bytes and nodes:
        nodes = nodes[: len(nodes) // 2]
        slim["nodes"] = nodes
        payload = json.dumps(slim, default=str)
    if len(payload.encode()) > max_bytes:
        slim = {"success": bool(result.get("success")),
                "errors": [log_scrub.scrub_urls(str(e))[:500] for e in (result.get("errors") or [])][:5],
                "nodes": [], "git_sha": result.get("git_sha"),
                "source_tree_sha": result.get("source_tree_sha"), "_truncated": True}
        payload = json.dumps(slim, default=str)
    return payload


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    git_url = os.environ["RUNNER_GIT_URL"]
    git_ref = os.environ["RUNNER_GIT_REF"]
    # DBT-006: the commit frozen when the run was submitted. Empty only for a Job created by an older runner.
    git_sha = os.environ.get("RUNNER_GIT_SHA") or None
    git_token = os.environ.get("RUNNER_GIT_TOKEN") or None      # DBT-009: handed to git through its environment, never in the URL
    project_path = os.environ.get("RUNNER_PROJECT_PATH") or ""
    dq_blocks = json.loads(os.environ.get("RUNNER_DQ_SCHEMA_TESTS_JSON") or "{}")
    workspace_job_token = os.environ.get("RUNNER_WORKSPACE_JOB_TOKEN") or ""

    # Local import — run_service pulls in more of the app graph than this
    # stripped-down entrypoint needs; only its pure helpers are used here.
    from app.core.config import get_settings
    from app.services.artifact_store import get_artifact_store, put_run_result
    from app.services.run_service import _apply_dq_schema_tests, _resolve_job_file, _resolve_project_dir

    settings = get_settings()
    git_svc = get_git_service()
    ref = None
    try:
        clone_dir = git_svc.clone_for_run(git_url=git_url, git_ref=git_ref, run_id=args.run_id, sha=git_sha, token=git_token)
        try:
            sha = git_svc.head_sha(clone_dir)
        except Exception:  # noqa: BLE001
            sha = None
        if git_sha and sha != git_sha:
            raise RuntimeError(f"cloned commit {sha} is not the run's frozen commit {git_sha}")
        if args.kind in ("sql", "python"):
            job_file = _resolve_job_file(clone_dir, project_path)
            tree_sha = git_svc.tree_sha(clone_dir, job_file)
            project_dir = job_file.parent
            if args.kind == "python":
                from app.executors.python_job_executor import run_python_file
                result = run_python_file(py_path=job_file, engine=args.engine, run_id=str(args.run_id), git_sha=sha)
            elif workspace_job_token:             # a SQL job on a Workspace connection: the Workspace runs it, not this pod
                from app.executors.connection_sql_job import run_sql_file_on_connection
                result = run_sql_file_on_connection(sql_path=job_file, token=workspace_job_token)
            else:
                from app.executors.sql_job_executor import run_sql_file
                result = run_sql_file(engine=args.engine, sql_path=job_file)
        else:
            project_dir = _resolve_project_dir(clone_dir, project_path)
            tree_sha = git_svc.tree_sha(clone_dir, project_dir)     # before DQ rules touch the working tree (X-004)
            _apply_dq_schema_tests(dq_blocks, project_dir)

            result = get_dbt_executor().run_project(
                project_dir=project_dir, engine=args.engine, select=args.select, run_id=args.run_id,
            )
        result["git_sha"] = sha
        if tree_sha:
            result["source_tree_sha"] = tree_sha

        try:
            store = get_artifact_store(settings)
            if store is not None:
                ref = put_run_result(store, run_id=args.run_id, attempt=args.attempt,
                                     result=result, project_dir=project_dir)
        except Exception:  # noqa: BLE001 — fall back to the inline result below
            log.exception("artifact_store_write_failed", run_id=str(args.run_id))
            ref = None
    finally:
        git_svc.cleanup(args.run_id)

    if ref is not None:
        print(f"{RESULT_REF} " + json.dumps({**ref, "success": bool(result.get("success"))}))
    else:
        print(RESULT_START)
        print(size_capped_inline(result, settings.runner_inline_result_max_bytes))
        print(RESULT_END)
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
