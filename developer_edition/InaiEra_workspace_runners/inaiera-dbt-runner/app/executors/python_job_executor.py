"""Python jobs: run one .py script of the repository.

    1. If a `requirements.txt` sits NEXT TO the script, a fresh virtualenv is built for this run and the requirements installed into
       it (so a job's packages never leak into another job or into the runner). Without one the script runs with the runner's
       own interpreter and packages. The interpreter is the runner's: one fixed Python version (3.11).
    2. The script runs as a child process in its own directory, in its own process group, with a hard timeout (the group is killed).

The script gets a minimal, explicit environment and NOTHING of the runner's: no signing secrets, no Git token, no database URL of the
runner's own. It is handed the run's warehouse engine, because that is what a job is for:

    INAIERA_RUN_ID, INAIERA_GIT_SHA, INAIERA_ENGINE
    pg_lake:  INAIERA_DATABASE_URL (postgresql://user:password@host:port/db) and the libpq set PGHOST PGPORT PGUSER PGPASSWORD PGDATABASE
    clickhouse: INAIERA_CLICKHOUSE_HOST, _PORT, _SECURE (true | false), _USER, _DATABASE and _PASSWORD when the runner has one
    trino:    INAIERA_TRINO_HOST, _PORT, _CATALOG, _HTTP_SCHEME, _AUTH (jwt | none), and INAIERA_TRINO_JWT when the runner has one
              (or INAIERA_TRINO_USER when the runner's Trino takes no authentication, dev only)

The result has the shape of a dbt result (success, errors, nodes), so finalization, run events, Dagster and Dependents treat it like
any other run: one node per step ("step.1" installing requirements, if any; then the script), each with its output tail.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import structlog

from app.core.config import get_settings
from app.core.log_scrub import scrub_urls

log = structlog.get_logger()

MESSAGE_CHARS = 300


def engine_environment(engine: str, settings=None) -> dict[str, str]:
    """The warehouse connection details handed to the script (and only these)."""
    s = settings or get_settings()
    env: dict[str, str] = {"INAIERA_ENGINE": engine}
    if engine == "pg_lake":
        u = urlparse(s.pg_lake_dsn or "")
        if u.hostname:
            user, password, dbname = unquote(u.username or ""), unquote(u.password or ""), (u.path or "/").lstrip("/") or "postgres"
            env.update({"INAIERA_DATABASE_URL": s.pg_lake_dsn or "", "PGHOST": u.hostname, "PGPORT": str(u.port or 5432),
                        "PGUSER": user, "PGPASSWORD": password, "PGDATABASE": dbname})
    elif engine == "trino":
        if s.trino_host:
            env.update({"INAIERA_TRINO_HOST": s.trino_host, "INAIERA_TRINO_PORT": str(s.trino_port),
                        "INAIERA_TRINO_CATALOG": s.trino_catalog, "INAIERA_TRINO_HTTP_SCHEME": s.trino_http_scheme})
        env["INAIERA_TRINO_AUTH"] = s.trino_auth_method
        if s.trino_auth_method == "none":
            env["INAIERA_TRINO_USER"] = s.trino_user           # a dev Trino without authentication: a user name only
        elif os.environ.get("TRINO_JWT"):
            env["INAIERA_TRINO_JWT"] = os.environ["TRINO_JWT"]
    elif engine == "clickhouse":
        if s.clickhouse_host:
            env.update({"INAIERA_CLICKHOUSE_HOST": s.clickhouse_host, "INAIERA_CLICKHOUSE_PORT": str(s.clickhouse_port),
                        "INAIERA_CLICKHOUSE_SECURE": "true" if s.clickhouse_secure else "false",
                        "INAIERA_CLICKHOUSE_USER": s.clickhouse_user, "INAIERA_CLICKHOUSE_DATABASE": s.clickhouse_database})
            password = s.clickhouse_password or os.environ.get("CLICKHOUSE_PASSWORD")
            if password:
                env["INAIERA_CLICKHOUSE_PASSWORD"] = password
    return env


def _base_environment(work: Path, run_id: str | None, git_sha: str | None, engine: str, settings) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(work / "home"), "TMPDIR": str(work / "tmp"), "LANG": "C.UTF-8",
        "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_NO_INPUT": "1",
        "INAIERA_JOB_KIND": "python", "INAIERA_RUN_ID": run_id or "", "INAIERA_GIT_SHA": git_sha or "",
    }
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        if os.environ.get(name):                     # how this pod reaches the package index, nothing more
            env[name] = os.environ[name]
    if settings.pip_index_url:
        env["PIP_INDEX_URL"] = settings.pip_index_url
    return env


def _secret_values(env: dict[str, str]) -> list[str]:
    return [env[k] for k in ("PGPASSWORD", "INAIERA_TRINO_JWT", "INAIERA_CLICKHOUSE_PASSWORD") if env.get(k)]


def _clean(text: str, secrets: list[str], limit: int) -> str:
    text = scrub_urls(text)
    for value in secrets:
        text = text.replace(value, "***")
    return text if len(text) <= limit else "…" + text[-limit:]


def _run(argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int) -> tuple[int | None, str, bool]:
    """(exit code or None if killed, combined output, timed out). The whole process group is killed on timeout."""
    with subprocess.Popen(argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                          text=True, errors="replace", start_new_session=True) as proc:
        try:
            out, _ = proc.communicate(timeout=timeout)
            return proc.returncode, out or "", False
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            out, _ = proc.communicate()
            return None, out or "", True


def _node(i: int, name: str, status: str, message: str, seconds: float, output: str) -> dict[str, Any]:
    return {"node_id": f"step.{i}", "status": status, "message": message, "execution_time": seconds, "step": name, "output": output}


def run_python_file(*, py_path: Path, engine: str, run_id: str | None = None, git_sha: str | None = None) -> dict[str, Any]:
    settings = get_settings()
    work = Path(tempfile.mkdtemp(prefix="pyjob-"))
    try:
        (work / "home").mkdir()
        (work / "tmp").mkdir()
        env = _base_environment(work, run_id, git_sha, engine, settings)
        env.update(engine_environment(engine, settings))
        secrets = _secret_values(env)
        limit = settings.python_job_output_max_chars
        nodes: list[dict[str, Any]] = []
        interpreter = sys.executable
        requirements = py_path.parent / "requirements.txt"

        if requirements.is_file():
            started = time.monotonic()
            venv_dir = work / "venv"
            code, out, timed_out = _run([sys.executable, "-m", "venv", str(venv_dir)], cwd=work, env=env, timeout=120)
            if code == 0:
                code, more, timed_out = _run([str(venv_dir / "bin" / "pip"), "install", "-r", str(requirements)], cwd=py_path.parent,
                                             env=env, timeout=settings.python_job_install_timeout_seconds)
                out += more
            took = time.monotonic() - started
            output = _clean(out, secrets, limit)
            if code != 0:
                reason = (f"installing requirements.txt timed out after {settings.python_job_install_timeout_seconds}s" if timed_out
                          else f"installing requirements.txt failed (exit code {code})")
                nodes.append(_node(1, "install requirements", "error", reason, took, output))
                nodes.append(_node(2, "run script", "skipped", "not run: the requirements could not be installed", 0.0, ""))
                return {"success": False, "nodes": nodes, "errors": [f"{reason}: {_clean(out, secrets, 600).strip()[-600:]}"]}
            nodes.append(_node(1, "install requirements", "success", "installed requirements.txt", took, output))
            interpreter = str(venv_dir / "bin" / "python")

        index = len(nodes) + 1
        started = time.monotonic()
        code, out, timed_out = _run([interpreter, "-u", py_path.name], cwd=py_path.parent, env=env, timeout=settings.python_job_timeout_seconds)
        took = time.monotonic() - started
        output = _clean(out, secrets, limit)
        if code == 0:
            nodes.append(_node(index, "run script", "success", f"exit code 0 · {took:.1f}s", took, output))
            return {"success": True, "nodes": nodes, "errors": []}
        reason = (f"the script ran longer than {settings.python_job_timeout_seconds}s and was stopped" if timed_out
                  else f"the script failed (exit code {code if code is not None and code >= 0 else f'signal {-code}' if code else 'unknown'})")
        nodes.append(_node(index, "run script", "error", reason, took, output))
        tail = _clean(out, secrets, 800).strip()
        return {"success": False, "nodes": nodes, "errors": [reason + (f": {tail[-800:]}" if tail else "")]}
    except Exception as e:  # noqa: BLE001 — a setup problem is the run's failure, with its reason
        log.warning("python_job_failed_to_start", error=type(e).__name__)
        return {"success": False, "errors": [f"could not run the Python job: {e}"], "nodes": []}
    finally:
        shutil.rmtree(work, ignore_errors=True)
