"""dbt executor.

Two entry points:
  - validate_model(): parse + compile + dry-run a single candidate model in
    a throwaway scratch project. Called by Workspace's codegen chat during
    its generate -> validate -> fix loop (POST /api/v1/validate). Fast,
    no materialization, no project registration required.
  - run_project(): clone a registered project at its pinned ref and run a
    full `dbt build` (or a `--select` subset) against its configured
    engine. Called by the run service for both manual and scheduled runs.

Engine dialect lives entirely in _profiles_yml() — everything above that
(the dbt invocation itself, result parsing) is engine-agnostic, mirroring
the dialect-router design used in Workspace's codegen backends.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from pathlib import Path
from typing import Any

import structlog
import yaml
from dbt.cli.main import dbtRunner

from app.core.config import get_settings
from app.executors import openlineage_emitter

log = structlog.get_logger()


def _force_profile(project_dir: Path, profile: str) -> None:
    """Rewrite the `profile:` key in a cloned project's dbt_project.yml so it
    uses our generated profile, preserving the rest of the project config."""
    pf = project_dir / "dbt_project.yml"
    if not pf.exists():
        return
    text = pf.read_text()
    if re.search(r"(?m)^\s*profile\s*:", text):
        text = re.sub(r"(?m)^(\s*profile\s*:).*$", rf"\1 '{profile}'", text)
    else:
        text += f"\nprofile: '{profile}'\n"
    pf.write_text(text)


def _profiles_yml(engine: str, schema: str = "public") -> str:
    settings = get_settings()
    if engine == "trino":
        if not settings.trino_host:
            raise ValueError("the Trino engine is not configured on this runner: set TRINO_HOST (and TRINO_PORT / TRINO_CATALOG / TRINO_HTTP_SCHEME)")
        auth = ('      method: none\n      user: "' + settings.trino_user + '"' if settings.trino_auth_method == "none"
                else '      method: jwt\n      jwt_token: "{{ env_var(\'TRINO_JWT\', \'\') }}"')
        return f"""
inaiera:
  target: prod
  outputs:
    prod:
      type: trino
      host: {settings.trino_host}
      port: {settings.trino_port}
      database: {settings.trino_catalog}
      schema: {schema}
      http_scheme: {settings.trino_http_scheme}
      threads: 8
{auth}
"""
    if engine == "pg_lake":
        # dbt-postgres needs discrete host/port/user/password/dbname (not a DSN).
        from urllib.parse import urlparse, unquote
        u = urlparse(settings.pg_lake_dsn or "")
        return f"""
inaiera:
  target: prod
  outputs:
    prod:
      type: postgres
      host: {u.hostname or 'localhost'}
      port: {u.port or 5432}
      user: {unquote(u.username or '')}
      password: "{unquote(u.password or '')}"
      dbname: {(u.path or '/').lstrip('/') or 'postgres'}
      schema: {schema}
      threads: 4
"""
    if engine == "clickhouse":
        if not settings.clickhouse_host:
            raise ValueError("the ClickHouse engine is not configured on this runner: set CLICKHOUSE_HOST (and CLICKHOUSE_PORT / CLICKHOUSE_USER / CLICKHOUSE_PASSWORD)")
        # In dbt-clickhouse the schema IS the ClickHouse database. The default schema ("public") is Postgres/Trino naming: on ClickHouse
        # models land in the configured database instead (a model can still name its own with `schema=`). The password stays out of the
        # file (read from the env).
        database = settings.clickhouse_database if schema == "public" else schema
        return f"""
inaiera:
  target: prod
  outputs:
    prod:
      type: clickhouse
      host: {settings.clickhouse_host}
      port: {settings.clickhouse_port}
      secure: {"true" if settings.clickhouse_secure else "false"}
      user: "{settings.clickhouse_user}"
      password: "{{{{ env_var('CLICKHOUSE_PASSWORD', '') }}}}"
      schema: {database}
      threads: 4
"""
    raise ValueError(f"unsupported engine: {engine}")


class DependencyError(Exception):
    """The project's dependencies do not meet what its rules need. Raised before dbt runs; the run fails with this message."""


_EXACT_VERSION = re.compile(r"^\d+\.\d+\.\d+([-+][0-9A-Za-z.\-]+)?$")
_FULL_SHA = re.compile(r"^[0-9a-fA-F]{40}$")


def _is_dbt_utils(entry: dict) -> bool:
    ident = " ".join(str(entry.get(k, "")) for k in ("package", "git", "local", "name"))
    return "dbt_utils" in ident or "dbt-utils" in ident


def _entry_is_pinned(entry: dict) -> bool:
    """An exactly pinned dependency: a hub version without any range, a git revision that is a full commit SHA, or a local path."""
    if "local" in entry:
        return True
    if "git" in entry:
        return bool(_FULL_SHA.match(str(entry.get("revision", ""))))
    version = entry.get("version")
    versions = version if isinstance(version, list) else [version]
    return len(versions) == 1 and isinstance(versions[0], str) and bool(_EXACT_VERSION.match(versions[0]))


def _require_pinned_dbt_utils(project_dir: Path) -> bool:
    """Whether `dbt deps` is warranted, after checking that a project whose tests use dbt_utils pins it EXACTLY (DBT-008).

    DQ renders range / regex / freshness / row_count / custom_sql as dbt_utils tests. The runner used to add `dbt-labs/dbt_utils` with a broad
    version range to the customer's packages.yml when it was missing: it silently changed the project's dependency model, and an execution-time
    network resolution made two runs of the same commit able to use different package content. Now the runner NEVER edits the customer's
    files. If a schema file uses dbt_utils, `packages.yml` must declare it pinned (a hub version without a range, a git revision that is a
    full SHA, or a local path); otherwise the run fails before dbt starts, saying what to add. Projects that never use dbt_utils are untouched.
    """
    uses_it = any(
        "dbt_utils." in path.read_text(errors="ignore")
        for pattern in ("*.yml", "*.yaml")
        for path in project_dir.rglob(pattern)
        if path.is_file() and path.name not in ("packages.yml", "dependencies.yml") and "dbt_packages" not in path.parts
    )
    if not uses_it:
        return False

    packages = project_dir / "packages.yml"
    try:
        doc = yaml.safe_load(packages.read_text()) or {} if packages.exists() else {}
    except (yaml.YAMLError, OSError):
        doc = {}
    entries = [e for e in (doc.get("packages") or []) if isinstance(e, dict)]
    declared = [e for e in entries if _is_dbt_utils(e)]
    add = "  - package: dbt-labs/dbt_utils\n    version: 1.3.0     # an exact version of your choice"
    if not declared:
        raise DependencyError(
            "this project's tests use dbt_utils (Data Quality rules render to dbt_utils macros) but packages.yml does not declare it. "
            "The runner does not modify your project's dependencies: add an exactly pinned entry to packages.yml:\n" + add)
    if not any(_entry_is_pinned(e) for e in declared):
        raise DependencyError(
            "packages.yml declares dbt_utils with a version RANGE or an unpinned reference, so two runs of the same commit could use "
            "different package content. Pin it exactly (a hub version such as 1.3.0, a git revision that is a full commit SHA, or a "
            "local path):\n" + add)
    return True


def _inject_openlineage_env(settings) -> dict[str, str]:
    """Build env-var overrides that activate dbt-openlineage.

    dbt-openlineage reads OPENLINEAGE_URL and OPENLINEAGE_NAMESPACE_NAME from
    the process environment before the dbt invocation.  We set them via
    os.environ so they're inherited by dbtRunner (which runs in-process) and
    also by any subprocess spawned by the k8s Job executor.

    Returns a dict of the vars that were set so the caller can restore the
    prior values after the run if needed.
    """
    if not settings.openlineage_url:
        return {}

    injected: dict[str, str] = {}
    for key, val in {
        "OPENLINEAGE_URL":            settings.openlineage_url,
        "OPENLINEAGE_NAMESPACE_NAME": settings.openlineage_namespace,
    }.items():
        if key not in os.environ:
            os.environ[key] = val
            injected[key] = val
    return injected


def _restore_env(injected: dict[str, str]) -> None:
    for key in injected:
        os.environ.pop(key, None)


DQ_ASYNC_TAG = "dq_async"


def _has_dq_async_tests(project_dir: Path) -> bool:
    for path in list(project_dir.rglob("*.yml")) + list(project_dir.rglob("*.yaml")):
        try:
            if DQ_ASYNC_TAG in path.read_text():
                return True
        except OSError:
            continue
    return False


def run_async_dq_tests(project_dir: Path, profiles_dir: Path) -> dict:
    """The separate test job for DQ async-mode tests. Its outcome is reported
    but never fails the run."""
    try:
        res = _run_dbt(["test", "--select", f"tag:{DQ_ASYNC_TAG}"], project_dir, profiles_dir)
    except Exception as e:  # noqa: BLE001
        return {"success": False, "errors": [f"dbt test invocation failed: {e}"], "nodes": []}
    return {"success": bool(res.success),
            "errors": [] if res.success else _extract_errors(res),
            "nodes": _extract_node_results(res)}


def _run_dbt(args: list[str], project_dir: Path, profiles_dir: Path) -> Any:
    runner = dbtRunner()
    return runner.invoke([
        *args,
        "--project-dir", str(project_dir),
        "--profiles-dir", str(profiles_dir),
        "--no-partial-parse",
    ])


class DbtExecutor:
    # ---------------------------------------------------------------
    # Validate-only path — used by Workspace's codegen loop
    # ---------------------------------------------------------------
    def validate_model(self, *, path: str, sql: str, schema_yml: str | None, engine: str) -> dict:
        """Builds a minimal scratch dbt project containing exactly one
        model file (plus its schema.yml, if given), runs `dbt compile`
        against it, and returns whether it compiled cleanly. This is
        intentionally cheap — it does not need the rest of the project's
        models, since dbt only needs the referenced sources/refs to
        resolve, which for a from-scratch model under validation is
        usually just `{{ source(...) }}` calls the catalog already knows
        about. A `{{ ref(...) }}` to a model that doesn't exist in this
        scratch project will correctly fail validation — which is the
        desired behavior (it tells Claude to fix the reference)."""
        with tempfile.TemporaryDirectory(prefix="dbtrunner-validate-") as tmp:
            project_dir = Path(tmp) / "project"
            profiles_dir = Path(tmp) / "profiles"
            project_dir.mkdir()
            profiles_dir.mkdir()

            (project_dir / "dbt_project.yml").write_text(
                "name: 'inaiera_scratch'\n"
                "version: '1.0.0'\n"
                "profile: 'inaiera'\n"
                "model-paths: ['models']\n"
            )
            (profiles_dir / "profiles.yml").write_text(_profiles_yml(engine))

            models_dir = project_dir / "models"
            models_dir.mkdir()
            model_path = models_dir / Path(path).name
            model_path.write_text(sql)
            if schema_yml:
                (models_dir / "schema.yml").write_text(schema_yml)

            try:
                result = _run_dbt(["compile"], project_dir, profiles_dir)
            except Exception as e:
                log.exception("dbt_validate_crashed")
                return {"valid": False, "errors": [f"dbt invocation failed: {e}"]}

            if not result.success:
                errors = _extract_errors(result)
                return {"valid": False, "errors": errors or ["dbt compile failed"]}

            compiled_sql = _extract_compiled_sql(result, model_path.stem)
            return {"valid": True, "errors": [], "compiled_sql": compiled_sql}

    # ---------------------------------------------------------------
    # Full project run
    # ---------------------------------------------------------------
    def run_project(
        self, *, project_dir: Path, engine: str, select: str | None,
        run_id: uuid.UUID | None = None,
    ) -> dict:
        """Runs `dbt build` (or a --select subset) and returns a summary
        plus the manifest (for lineage extraction) and run_results (for
        per-node status). Caller persists RunEvent rows from these."""
        with tempfile.TemporaryDirectory(prefix="dbtrunner-profiles-") as tmp:
            profiles_dir = Path(tmp)
            (profiles_dir / "profiles.yml").write_text(_profiles_yml(engine))

            # The generated profiles.yml is named 'inaiera'; point the cloned
            # project's dbt_project.yml at it (projects normally declare their
            # own profile name, e.g. 'jaffle_shop', which we don't provide).
            _force_profile(project_dir, "inaiera")

            # DQ renders range / regex / freshness / row_count / custom_sql as
            # dbt_utils tests. That macro lives in a package the PROJECT must pin
            # (DBT-008): the runner checks, and fails clearly if it is missing or
            # unpinned, rather than editing the customer's packages.yml.
            try:
                needs_deps = _require_pinned_dbt_utils(project_dir)
            except DependencyError as e:
                log.warning("dbt_utils_not_pinned", error=str(e))
                return {"success": False, "errors": [str(e)], "nodes": []}
            if needs_deps:
                deps = _run_dbt(["deps"], project_dir, profiles_dir)
                if not deps.success:
                    # The pinned package could not be installed: building without it would only fail on "macro not found".
                    message = "dbt deps failed for the project's pinned dependencies: " + (
                        "; ".join(_extract_errors(deps)) or str(getattr(deps, "exception", "") or "unknown error"))
                    return {"success": False, "errors": [message], "nodes": []}

            # DQ-MODEL-002 "async" contract: DQ tags async-mode tests
            # `dq_async`. They never run inside (or fail) the build; they run
            # afterwards as a separate `dbt test` job (see below).
            args = ["build"]
            if select:
                args += ["--select", select]
            has_async = _has_dq_async_tests(project_dir)
            if has_async:
                args += ["--exclude", f"tag:{DQ_ASYNC_TAG}"]

            settings = get_settings()
            injected = _inject_openlineage_env(settings)
            try:
                result = _run_dbt(args, project_dir, profiles_dir)
            except Exception as e:
                log.exception("dbt_run_crashed")
                return {"success": False, "errors": [f"dbt invocation failed: {e}"], "nodes": []}
            finally:
                _restore_env(injected)

            nodes = _extract_node_results(result)
            manifest = _read_manifest(project_dir)

            # Build (never send — see RUN-DBT-008) the lineage/quality/schema
            # event from dbt's own artifacts, while the working directory
            # still exists. Deliberately after the build and independent of
            # its success: a failed run's test results are exactly the ones
            # the catalog most needs. The caller persists this into the
            # durable metadata outbox for actual delivery — this function
            # makes no network calls of its own, so it works the same
            # in-process or inside an isolated k8s Job pod with no DB access.
            lineage_event = None
            if run_id is not None:
                lineage_event = openlineage_emitter.read_and_build_event(
                    project_dir=project_dir, run_id=run_id, namespace=settings.openlineage_namespace)

            async_tests = None
            if has_async:
                async_tests = run_async_dq_tests(project_dir, profiles_dir)

            return {
                "success": bool(result.success),
                "errors": [] if result.success else _extract_errors(result),
                "nodes": nodes,
                "manifest": manifest,
                "lineage_event": lineage_event,
                # Separate test job; never affects `success`.
                "async_tests": async_tests,
            }


def _extract_errors(result: Any) -> list[str]:
    """What went wrong: the failing nodes' messages, else dbt's own exception (a parse or compilation error stops dbt before
    any node runs, so there are no node results at all), else a generic line — a failed run never has an empty reason."""
    errors: list[str] = []
    res = getattr(result, "result", None)
    for r in getattr(res, "results", []) or []:
        status = getattr(r, "status", None)
        if status and str(status).lower() in ("error", "fail"):
            msg = getattr(r, "message", None) or str(getattr(r, "status", "error"))
            errors.append(msg)
    if not errors and getattr(result, "exception", None):
        errors.append(str(result.exception))
    if not errors and not getattr(result, "success", False):
        errors.append("dbt failed without reporting a reason (see the run's logs)")
    return errors


def _extract_node_results(result: Any) -> list[dict]:
    nodes: list[dict] = []
    res = getattr(result, "result", None)
    if res is None:
        return nodes
    for r in getattr(res, "results", []) or []:
        nodes.append({
            "node_id": getattr(getattr(r, "node", None), "unique_id", None),
            "status": str(getattr(r, "status", "unknown")),
            "message": getattr(r, "message", None),
            "execution_time": getattr(r, "execution_time", None),
        })
    return nodes


def _extract_compiled_sql(result: Any, model_name: str) -> str | None:
    res = getattr(result, "result", None)
    if res is None:
        return None
    for r in getattr(res, "results", []) or []:
        node = getattr(r, "node", None)
        if node is not None and getattr(node, "name", None) == model_name:
            return getattr(node, "compiled_code", None) or getattr(node, "compiled_sql", None)
    return None


def _read_manifest(project_dir: Path) -> dict | None:
    manifest_path = project_dir / "target" / "manifest.json"
    if not manifest_path.exists():
        return None
    try:
        return json.loads(manifest_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


_executor: DbtExecutor | None = None


def get_dbt_executor() -> DbtExecutor:
    global _executor
    if _executor is None:
        _executor = DbtExecutor()
    return _executor
