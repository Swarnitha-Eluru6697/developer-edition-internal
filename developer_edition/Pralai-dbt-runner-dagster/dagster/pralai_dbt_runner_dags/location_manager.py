"""Code-location manager: one Dagster gRPC code server per Git repo.

Run as the ``dagster-code`` container's command:  python -m pralai_dbt_runner_dags.location_manager

Why: the Dagster Jobs page groups jobs by *code location*, and a location is fixed when
its code server starts. To show a folder per repo with one job per dbt project, while
projects keep being registered at runtime, this process

  1. every SYNC_INTERVAL_SECONDS lists the projects in the dbt Runner and groups them by
     repo; each repo gets its own gRPC server (``location_defs`` with DBT_LOCATION=<repo>)
     on ports 4001+, restarted when that repo's set of projects changes,
  2. rewrites the generated workspace.yaml when the set of repos changes and asks the
     webserver to reload (GraphQL ``reloadWorkspace`` / ``reloadRepositoryLocation``).

A reload only counts when the webserver SAYS it worked (DG-014): the HTTP status, GraphQL ``errors`` and the union
type Dagster answers with (``PythonError``, ``RepositoryLocationNotFound``, a location that failed to load...) are all
checked. A failure is retried with bounded exponential backoff; one that still fails is remembered ("owed") and tried
again at the start of every sync pass until it works. ``/metrics`` (LOCATION_MANAGER_METRICS_PORT, default 9102)
exports ``dagster_location_reload_total{kind,result}``, ``dagster_location_reload_pending{kind}`` and
``dagster_location_reload_last_success_timestamp_seconds{kind}``, and every give-up logs
``location_manager.reload_failed`` at error level. See DEPLOYMENT.md for an alert rule.

There is no generic location: every dbt project is a job in its repo's folder (each job can be
launched by hand from its Launchpad). ``definitions.py`` (a generic job) is only used by the
Helm chart's single-location deployment.

The webserver and daemon read the generated workspace.yaml from a shared volume.
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import structlog
import yaml

from pralai_dbt_runner_dags.location_defs import fetch_all_projects
from pralai_dbt_runner_dags.naming import group_by_location, location_fingerprint

log = structlog.get_logger()

BASE_PORT = 4001
WORKSPACE_FILE = os.environ.get("DAGSTER_GENERATED_WORKSPACE", "/dagster-config/generated/workspace.yaml")
CODE_HOST = os.environ.get("DAGSTER_CODE_SERVER_HOST", "dagster-code")
WEBSERVER_URL = os.environ.get("DAGSTER_WEBSERVER_URL", "http://dagster-webserver:3000")
SYNC_INTERVAL_SECONDS = float(os.environ.get("SYNC_INTERVAL_SECONDS", "30"))
HEALTH_TIMEOUT_SECONDS = 90.0

# DG-014: reload attempts per call, the first backoff, its cap, and a deadline for one call's retries.
RELOAD_ATTEMPTS = int(os.environ.get("LOCATION_RELOAD_ATTEMPTS", "4"))
RELOAD_BACKOFF_BASE = 1.0
RELOAD_BACKOFF_MAX = 8.0
RELOAD_DEADLINE_SECONDS = float(os.environ.get("LOCATION_RELOAD_DEADLINE_SECONDS", "60"))
GRAPHQL_TIMEOUT_SECONDS = 30.0
METRICS_PORT = int(os.environ.get("LOCATION_MANAGER_METRICS_PORT", "9102"))
_sleep = time.sleep   # a name of its own so tests can run the backoff without waiting

# What each mutation must answer for the reload to count, and how a failure is reported. Dagster returns most failures
# as HTTP 200 with a union member in `data` (PythonError, RepositoryLocationNotFound, ...), not in `errors`.
_WORKSPACE_MUTATION = (
    "mutation { reloadWorkspace { __typename ... on PythonError { message } ... on UnauthorizedError { message } } }")
_LOCATION_MUTATION = (
    "mutation($n: String!) { reloadRepositoryLocation(repositoryLocationName: $n) { __typename "
    "... on WorkspaceLocationEntry { name loadStatus locationOrLoadError { __typename ... on PythonError { message } } } "
    "... on PythonError { message } ... on UnauthorizedError { message } "
    "... on RepositoryLocationNotFound { message } ... on ReloadNotSupported { message } } }")
_NEVER_RETRY = {"UnauthorizedError", "ReloadNotSupported", "RepositoryLocationNotFound"}


@dataclass
class Server:
    location: str
    port: int
    proc: subprocess.Popen
    fingerprint: tuple = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested)
# ---------------------------------------------------------------------------
def plan_changes(
    running: dict[str, tuple], alive: dict[str, bool], desired: dict[str, tuple],
) -> tuple[list[str], list[str], list[str]]:
    """(to_start, to_restart, to_stop) location names.

    running: location -> fingerprint it was started with; alive: is its process still up;
    desired: location -> fingerprint of the projects registered now.
    """
    to_start = sorted(loc for loc in desired if loc not in running)
    to_restart = sorted(
        loc for loc, fp in desired.items()
        if loc in running and (running[loc] != fp or not alive.get(loc, False))
    )
    to_stop = sorted(loc for loc in running if loc not in desired)
    return to_start, to_restart, to_stop


# Dagster accepts any string as a code location name in workspace.yaml, but a name is also a URL path segment in the UI, a GraphQL argument
# and a key in the run history: keep it to letters, digits, `_`, `-` and `.`, starting with an alphanumeric (DG-013).
_LOCATION_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*$")
_UNSAFE = re.compile(r"[^A-Za-z0-9_.\-]")


def sanitize_location_name(name: str) -> str:
    """`name` made valid: every character outside [A-Za-z0-9_.-] becomes `-`, and it starts with an alphanumeric. A name that is already valid is
    returned unchanged (location_name() produces valid names; this is the last line of defence for the generated file)."""
    if _LOCATION_NAME.match(name or ""):
        return name
    cleaned = _UNSAFE.sub("-", name or "").lstrip("-._") or "location"
    return cleaned[:200]


def workspace_document(host: str, entries: list[tuple[str, int]]) -> dict:
    """The workspace.yaml content as a plain Python object (serialised by yaml.safe_dump, never by string interpolation)."""
    if not isinstance(host, str) or not host:
        raise ValueError("workspace host must be a non-empty string")
    load_from = []
    seen: set[str] = set()
    for name, port in entries:
        clean = sanitize_location_name(name)
        if clean in seen:
            raise ValueError(f"two code locations would be named {clean!r} (from {name!r})")
        seen.add(clean)
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise ValueError(f"invalid port {port!r} for location {name!r}")
        load_from.append({"grpc_server": {"host": host, "port": port, "location_name": clean}})
    return {"load_from": load_from}


def render_workspace(host: str, entries: list[tuple[str, int]]) -> str:
    """workspace.yaml text for (location name, port) pairs: a Python object through yaml.safe_dump, so quotes, backslashes, colons and
    other special characters in a repository-derived value can never produce invalid or ambiguous YAML (DG-013)."""
    header = "# GENERATED by location_manager.py — do not edit; rewritten when repos change.\n"
    return header + yaml.safe_dump(workspace_document(host, entries), sort_keys=False, default_flow_style=False, allow_unicode=True)


class ReloadError(Exception):
    """A reload that did not happen. `retryable`: trying the same request again could work."""

    def __init__(self, message: str, *, retryable: bool = True, unknown_location: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.unknown_location = unknown_location


def check_reload_response(resp: httpx.Response, field_name: str, ok_type: str) -> None:
    """Raise ReloadError unless `resp` says the reload worked (pure; unit-tested)."""
    if resp.status_code >= 500 or resp.status_code in (408, 429):
        raise ReloadError(f"HTTP {resp.status_code} from the webserver")
    if resp.status_code >= 400:
        raise ReloadError(f"HTTP {resp.status_code} from the webserver: {resp.text[:200]}", retryable=False)
    try:
        body = resp.json()
    except ValueError:
        raise ReloadError("the webserver did not answer with JSON") from None
    if not isinstance(body, dict):
        raise ReloadError("the webserver's answer is not a GraphQL response")
    if body.get("errors"):
        messages = "; ".join(str(e.get("message", e)) if isinstance(e, dict) else str(e) for e in body["errors"])
        raise ReloadError(f"GraphQL errors: {messages[:300]}")
    result = (body.get("data") or {}).get(field_name)
    if not isinstance(result, dict):
        raise ReloadError(f"no {field_name} result in the GraphQL response")
    typename = result.get("__typename")
    if typename != ok_type:
        raise ReloadError(f"{typename}: {str(result.get('message', ''))[:300]}".rstrip(": "),
                          retryable=typename not in _NEVER_RETRY, unknown_location=typename == "RepositoryLocationNotFound")
    load = result.get("locationOrLoadError")
    if isinstance(load, dict) and load.get("__typename") != "RepositoryLocation":
        raise ReloadError(f"the location failed to load after the reload: {str(load.get('message', load))[:300]}")


class ReloadMetrics:
    """Prometheus text-format counters and gauges for the reloads (no client library: the code container has none)."""
    KINDS = ("workspace", "location")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts = {(k, r): 0 for k in self.KINDS for r in ("success", "failure")}
        self._last_success: dict[str, float] = {}
        self._pending = {k: 0 for k in self.KINDS}

    def record(self, kind: str, ok: bool) -> None:
        with self._lock:
            self._counts[(kind, "success" if ok else "failure")] += 1
            if ok:
                self._last_success[kind] = time.time()

    def set_pending(self, kind: str, n: int) -> None:
        with self._lock:
            self._pending[kind] = n

    def pending(self, kind: str) -> int:
        with self._lock:
            return self._pending[kind]

    def render(self) -> str:
        with self._lock:
            out = ["# HELP dagster_location_reload_total Reloads the location manager asked the webserver for, after retries.",
                   "# TYPE dagster_location_reload_total counter"]
            out += [f'dagster_location_reload_total{{kind="{k}",result="{r}"}} {n}' for (k, r), n in sorted(self._counts.items())]
            out += ["# HELP dagster_location_reload_pending Reloads still owed (failed and not yet retried successfully).",
                    "# TYPE dagster_location_reload_pending gauge"]
            out += [f'dagster_location_reload_pending{{kind="{k}"}} {n}' for k, n in sorted(self._pending.items())]
            out += ["# HELP dagster_location_reload_last_success_timestamp_seconds Unix time of the last successful reload.",
                    "# TYPE dagster_location_reload_last_success_timestamp_seconds gauge"]
            out += [f'dagster_location_reload_last_success_timestamp_seconds{{kind="{k}"}} {t:.0f}'
                    for k, t in sorted(self._last_success.items())]
        return "\n".join(out) + "\n"


METRICS = ReloadMetrics()


def start_metrics_server(port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path.split("?")[0] != "/metrics":
                self.send_error(404)
                return
            body = METRICS.render().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)  # inside the container network, never published
    threading.Thread(target=server.serve_forever, name="location-manager-metrics", daemon=True).start()
    return server


# ---------------------------------------------------------------------------
# Process supervision
# ---------------------------------------------------------------------------
class Manager:
    def __init__(self) -> None:
        self.servers: dict[str, Server] = {}
        self.stopping = False
        # DG-014: reloads that failed and are still owed (a workspace reload also covers every location).
        self.pending_workspace = False
        self.pending_locations: set[str] = set()

    # -- starting / stopping ------------------------------------------------
    def _spawn(self, module: str, port: int, env_extra: dict[str, str]) -> subprocess.Popen:
        return subprocess.Popen(
            ["dagster", "api", "grpc", "--module-name", module, "--attribute", "defs",
             "--host", "0.0.0.0", "--port", str(port)],
            env={**os.environ, **env_extra},
        )

    def _wait_healthy(self, port: int, proc: subprocess.Popen) -> bool:
        deadline = time.monotonic() + HEALTH_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return False
            ok = subprocess.run(
                ["dagster", "api", "grpc-health-check", "-p", str(port)],
                capture_output=True,
            ).returncode == 0
            if ok:
                return True
            time.sleep(1.0)
        return False

    def _free_port(self) -> int:
        used = {s.port for s in self.servers.values()}
        port = BASE_PORT
        while port in used:
            port += 1
        return port

    def start(self, location: str, fingerprint: tuple, port: int | None = None) -> bool:
        port = port or self._free_port()
        proc = self._spawn("pralai_dbt_runner_dags.location_defs", port, {"DBT_LOCATION": location})
        if not self._wait_healthy(port, proc):
            log.error("location_manager.start_failed", location=location, port=port)
            self._terminate(proc)
            self.servers.pop(location, None)
            return False
        self.servers[location] = Server(location, port, proc, fingerprint)
        log.info("location_manager.started", location=location, port=port)
        return True

    @staticmethod
    def _terminate(proc: subprocess.Popen) -> None:
        if proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    def stop(self, location: str) -> None:
        srv = self.servers.pop(location, None)
        if srv:
            self._terminate(srv.proc)
            log.info("location_manager.stopped", location=location)

    def restart(self, location: str, fingerprint: tuple) -> bool:
        port = self.servers[location].port if location in self.servers else None
        self.stop(location)
        return self.start(location, fingerprint, port)

    # -- workspace + webserver ---------------------------------------------
    def write_workspace(self) -> None:
        entries = sorted((s.location, s.port) for s in self.servers.values())
        os.makedirs(os.path.dirname(WORKSPACE_FILE), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(WORKSPACE_FILE), prefix=".workspace-")
        with os.fdopen(fd, "w") as f:
            f.write(render_workspace(CODE_HOST, entries))
        os.replace(tmp, WORKSPACE_FILE)  # atomic: readers never see a half-written file
        log.info("location_manager.workspace_written", locations=[n for n, _ in entries])

    def _reload(self, kind: str, location: str | None, query: str, variables: dict[str, Any] | None,
                field_name: str, ok_type: str) -> bool:
        """POST one reload mutation and check the answer; retry a failure that could work with bounded exponential
        backoff. True only if the webserver confirmed it. Never raises: a failure is logged, counted and owed."""
        started = time.monotonic()
        error: ReloadError | None = None
        attempts = 0
        for attempt in range(1, RELOAD_ATTEMPTS + 1):
            attempts = attempt
            try:
                with httpx.Client(timeout=GRAPHQL_TIMEOUT_SECONDS) as client:
                    resp = client.post(f"{WEBSERVER_URL}/graphql", json={"query": query, "variables": variables or {}})
                check_reload_response(resp, field_name, ok_type)
                METRICS.record(kind, True)
                log.info("location_manager.reloaded", kind=kind, location=location, attempts=attempt)
                return True
            except ReloadError as e:
                error = e
            except httpx.HTTPError as e:
                error = ReloadError(f"{type(e).__name__}: {e}")
            if not error.retryable or attempt == RELOAD_ATTEMPTS:
                break
            delay = min(RELOAD_BACKOFF_BASE * 2 ** (attempt - 1), RELOAD_BACKOFF_MAX)
            if time.monotonic() - started + delay > RELOAD_DEADLINE_SECONDS:
                break
            log.warning("location_manager.reload_retrying", kind=kind, location=location, attempt=attempt,
                        error=str(error), retry_in_seconds=delay)
            _sleep(delay)
        assert error is not None
        METRICS.record(kind, False)
        log.error("location_manager.reload_failed", kind=kind, location=location, attempts=attempts, error=str(error),
                  owed=True)
        if error.unknown_location:
            self.pending_workspace = True           # the webserver does not know it yet: only a workspace reload can add it
        elif kind == "location" and location:
            self.pending_locations.add(location)
        else:
            self.pending_workspace = True
        self._publish_pending()
        return False

    def _publish_pending(self) -> None:
        METRICS.set_pending("workspace", 1 if self.pending_workspace else 0)
        METRICS.set_pending("location", len(self.pending_locations))

    def reload_workspace(self) -> bool:
        ok = self._reload("workspace", None, _WORKSPACE_MUTATION, None, "reloadWorkspace", "Workspace")
        if ok:
            self.pending_workspace = False
            self.pending_locations.clear()          # reloading the workspace reloads every location
            self._publish_pending()
        return ok

    def reload_location(self, location: str) -> bool:
        ok = self._reload("location", location, _LOCATION_MUTATION, {"n": location},
                          "reloadRepositoryLocation", "WorkspaceLocationEntry")
        if ok:
            self.pending_locations.discard(location)
            self._publish_pending()
        return ok

    def flush_pending_reloads(self) -> None:
        """Retry the reloads a previous pass could not complete. One exhausted attempt ends the pass: if the webserver
        is down, trying every owed location would only stall the loop."""
        if self.pending_workspace:
            self.reload_workspace()
            return
        for loc in sorted(self.pending_locations):
            if not self.reload_location(loc) or self.pending_workspace:
                return

    # -- one sync pass ------------------------------------------------------
    def sync(self) -> None:
        self.flush_pending_reloads()  # reloads a previous pass could not complete (DG-014)
        try:
            projects = fetch_all_projects(attempts=1)
        except Exception as e:  # noqa: BLE001 — keep serving what we have
            log.warning("location_manager.fetch_failed", error=str(e))
            return
        desired = {loc: location_fingerprint(ps) for loc, ps in group_by_location(projects).items()}
        running = {loc: s.fingerprint for loc, s in self.servers.items()}
        alive = {loc: s.proc.poll() is None for loc, s in self.servers.items()}
        to_start, to_restart, to_stop = plan_changes(running, alive, desired)

        membership_changed = False
        for loc in to_start:
            membership_changed |= self.start(loc, desired[loc])
        for loc in to_stop:
            self.stop(loc)
            membership_changed = True
        for loc in to_restart:
            if self.restart(loc, desired[loc]):
                self.reload_location(loc)
            else:
                membership_changed = True  # it failed to come back: drop it from the workspace
        if membership_changed:
            self.write_workspace()
            self.reload_workspace()

    # -- main loop ----------------------------------------------------------
    def shutdown(self, *_: Any) -> None:
        self.stopping = True

    def run(self) -> int:
        signal.signal(signal.SIGTERM, self.shutdown)
        signal.signal(signal.SIGINT, self.shutdown)
        if METRICS_PORT > 0:
            try:
                start_metrics_server(METRICS_PORT)
                log.info("location_manager.metrics_listening", port=METRICS_PORT)
            except OSError as e:
                log.warning("location_manager.metrics_unavailable", port=METRICS_PORT, error=str(e))
        self.sync()
        self.write_workspace()  # the webserver and daemon wait for this file
        while not self.stopping:
            slept = 0.0
            while slept < SYNC_INTERVAL_SECONDS and not self.stopping:
                time.sleep(1.0)
                slept += 1.0
            if self.stopping:
                break
            self.sync()
        for loc in list(self.servers):
            self.stop(loc)
        return 0


def main() -> int:
    return Manager().run()


if __name__ == "__main__":
    sys.exit(main())
