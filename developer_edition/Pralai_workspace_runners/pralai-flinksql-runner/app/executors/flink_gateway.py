"""Flink SQL Gateway client.

The Flink SQL Gateway is a REST service provided by Apache Flink that
accepts multi-statement SQL sessions. This client wraps its REST API:
  v1/  — JobManager REST (job status, metrics, cancel, savepoints)
  v1/  — SQL Gateway (sessions, statements, results); Flink 1.19 serves V1/V2 only

Reference: https://nightlies.apache.org/flink/flink-docs-stable/docs/dev/table/sql-gateway/overview/

Job ID capture (RUN-FLINK-011): execute_statements() reads the exact Flink
job ID a submitting statement produced directly out of that statement's
own FINISHED operation result — never by listing every job in the cluster
afterwards and guessing which one is "ours" by name prefix or by picking
whichever happens to be RUNNING. See _extract_job_id_from_result().
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx
import structlog

from app.core.config import get_settings

log = structlog.get_logger()


# RUN-FLINK-012: the shared, lexically-aware splitter and statement
# classifiers live in app.core.sql_script (also used by validate_service);
# re-exported here for existing importers.
from app.core.sql_script import (  # noqa: E402
    is_job_submitting,
    leading_keyword,
    split_sql_script,
    statement_kind,
    strip_leading_comments as _strip_leading_comments,
)

# Flink's Long.MIN_VALUE "no watermark yet" sentinel.
_NO_WATERMARK = -9223372036854775808


class JobIdUnavailableError(RuntimeError):
    """A job-submitting statement FINISHED (so Flink accepted a job) but
    its job id could not be read from the operation result. The caller
    MUST NOT treat this as "nothing was submitted" (RUN-FLINK-011) — it
    has to locate the job by its unique pipeline name, cancel it, or
    leave the deployment for the reconciler."""

    def __init__(self, message: str, *, session_id: str, op_handle: str, statement: str):
        super().__init__(message)
        self.session_id = session_id
        self.op_handle = op_handle
        self.statement = statement


def _extract_job_id_from_result(payload: dict) -> str | None:
    """Pull the Flink job ID out of a FINISHED job-submitting statement's
    own operation result — the SQL Gateway's actual submission contract,
    not a guess made afterwards by scanning /v1/jobs (RUN-FLINK-011).

    Newer Flink SQL Gateway versions report it directly as a top-level
    "jobID" field on the fetch-results response. The always-available
    fallback is the single result row Flink's own TableResult produces
    for an INSERT/EXECUTE statement, whose one column is named "job id"
    (case/spacing exactly as Flink emits it) — so match that by name
    rather than assuming it's always at a fixed index.
    """
    job_id = payload.get("jobID") or payload.get("jobId")
    if job_id:
        return str(job_id)

    results = payload.get("results") or {}
    columns = results.get("columns") or []
    data = results.get("data") or []
    if not columns or not data:
        return None
    for idx, col in enumerate(columns):
        if str(col.get("name", "")).strip().lower() == "job id":
            fields = data[0].get("fields") or []
            if idx < len(fields) and fields[idx]:
                return str(fields[idx])
    return None


class FlinkGatewayClient:
    """Wraps the Flink SQL Gateway REST API (v1) and JobManager REST API (v1)."""

    def __init__(self) -> None:
        settings = get_settings()
        self._gw = settings.flink_gateway_url.rstrip("/")
        self._jm = settings.flink_rest_url.rstrip("/")
        self._timeout = settings.flink_gateway_timeout_seconds
        self._submit_timeout = settings.flink_submit_timeout_seconds

    # ── SQL Gateway session management ───────────────────────────────────

    async def create_session(self, session_name: str | None = None) -> str:
        """Create a new SQL Gateway session. Returns the session_handle."""
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            resp = await c.post(
                f"{self._gw}/v1/sessions",
                json={"sessionName": session_name or "inaiera-job-session",
                      "properties": {}},
            )
            resp.raise_for_status()
            return resp.json()["sessionHandle"]

    async def execute_statements(
        self, session_id: str, sql: str, *, on_submit=None,
    ) -> list[dict]:
        """Execute a multi-statement SQL block, one statement at a time.

        The SQL Gateway's /statements endpoint runs one statement per
        request, so the block is split with the shared lexically-aware
        splitter (RUN-FLINK-012) and submitted sequentially in the same
        session — DDL registers before the final INSERT/STATEMENT SET.

        on_submit (RUN-FLINK-013): awaited with (op_handle, statement)
        right after the Gateway accepts a JOB-SUBMITTING statement and
        before polling it, so the caller can durably record the operation
        handle before anything else can go wrong.

        Each result: {op_handle, state, statement, kind, flink_job_id}.
        Raises JobIdUnavailableError if a submitting statement FINISHED
        but its job id could not be read (RUN-FLINK-011).
        """
        statements = split_sql_script(sql)

        # Every INSERT submitted on its own becomes a separate Flink job (own
        # slot, own read of the source topic). Several plain INSERTs are
        # compiled into ONE statement set. Each INSERT is joined with a
        # newline BEFORE its `;`, so a trailing `-- comment` on an INSERT can
        # never swallow the separator.
        inserts = [st for st in statements if statement_kind(st) == "INSERT"]
        if len(inserts) > 1:
            others = [st for st in statements if statement_kind(st) != "INSERT"]
            statements = others + [
                "EXECUTE STATEMENT SET BEGIN\n" + "\n;\n".join(inserts) + "\n;\nEND"
            ]

        results: list[dict] = []
        async with httpx.AsyncClient(timeout=self._submit_timeout) as c:
            for stmt in statements:
                kind = statement_kind(stmt)
                submitting = is_job_submitting(stmt)
                resp = await c.post(
                    f"{self._gw}/v1/sessions/{session_id}/statements",
                    json={"statement": stmt},
                )
                resp.raise_for_status()
                op_handle = resp.json()["operationHandle"]
                if submitting and on_submit is not None:
                    await on_submit(op_handle, stmt)

                # Poll for completion. For an INSERT that starts a streaming job,
                # the operation FINISHES once the job is submitted.
                for _ in range(60):
                    status_resp = await c.get(
                        f"{self._gw}/v1/sessions/{session_id}/operations/{op_handle}/status",
                    )
                    status_resp.raise_for_status()
                    state = status_resp.json().get("status") or "RUNNING"
                    if state in ("FINISHED", "ERROR", "CANCELED", "CLOSED"):
                        if state == "ERROR":
                            err_resp = await c.get(
                                f"{self._gw}/v1/sessions/{session_id}/operations/{op_handle}/result/0",
                            )
                            error_msg = err_resp.json().get("errors", ["Unknown error"])
                            raise RuntimeError(f"Flink SQL error in `{stmt[:80]}`: {error_msg}")
                        break
                    await asyncio.sleep(1)
                else:
                    raise TimeoutError(f"Flink SQL statement timed out: {stmt[:80]}")

                # RUN-FLINK-011: the exact job id THIS statement submitted, from
                # the Gateway's own operation result — never by scanning
                # /v1/jobs. DDL has none.
                flink_job_id = None
                if state == "FINISHED" and submitting:
                    try:
                        result_resp = await c.get(
                            f"{self._gw}/v1/sessions/{session_id}/operations/{op_handle}/result/0",
                        )
                        if result_resp.status_code == 200:
                            flink_job_id = _extract_job_id_from_result(result_resp.json())
                    except httpx.HTTPError as e:
                        log.warning("flink_job_id_fetch_failed", op_handle=op_handle, error=str(e))
                    if not flink_job_id:
                        raise JobIdUnavailableError(
                            "Flink accepted the job-submitting statement but its job id "
                            "could not be read from the Gateway operation result",
                            session_id=session_id, op_handle=op_handle, statement=stmt[:200],
                        )

                results.append({
                    "op_handle": op_handle, "state": state, "statement": stmt[:80],
                    "kind": kind, "flink_job_id": flink_job_id,
                })
            return results

    async def get_operation_status(self, session_id: str, op_handle: str) -> str | None:
        """Gateway operation state, or None if the session/operation no
        longer exists there (e.g. the Gateway restarted)."""
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            resp = await c.get(
                f"{self._gw}/v1/sessions/{session_id}/operations/{op_handle}/status",
            )
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json().get("status")

    async def cancel_operation(self, session_id: str, op_handle: str) -> None:
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            await c.post(f"{self._gw}/v1/sessions/{session_id}/operations/{op_handle}/cancel")

    async def explain_sql(self, session_id: str, sql: str) -> dict:
        """Run EXPLAIN on a SQL statement to validate syntax without execution."""
        # Drop leading comment lines so EXPLAIN binds to the statement itself —
        # rendered quality checks always open with a `-- Rule: ...` line.
        body = _strip_leading_comments(sql)
        if body.upper().startswith("EXECUTE "):
            body = body[len("EXECUTE "):].lstrip()   # EXPLAIN STATEMENT SET / EXPLAIN INSERT
        explain_sql = body if body.upper().startswith("EXPLAIN") else f"EXPLAIN {body}"
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            resp = await c.post(
                f"{self._gw}/v1/sessions/{session_id}/statements",
                json={"statement": explain_sql},
            )
            if resp.status_code >= 400:
                return {"valid": False, "errors": [resp.text[:500]]}
            op_handle = resp.json()["operationHandle"]
            for _ in range(30):
                st = await c.get(
                    f"{self._gw}/v1/sessions/{session_id}/operations/{op_handle}/status",
                )
                state = st.json().get("status") or "RUNNING"
                if state == "FINISHED":
                    result = await c.get(
                        f"{self._gw}/v1/sessions/{session_id}/operations/{op_handle}/result/0",
                    )
                    return {"valid": True, "errors": [], "plan": result.json()}
                if state == "ERROR":
                    return {"valid": False, "errors": [st.json().get("errorMsg", "Compile error")]}
                await asyncio.sleep(0.5)
        return {"valid": False, "errors": ["Validate timed out"]}

    async def close_session(self, session_id: str) -> None:
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            await c.delete(f"{self._gw}/v1/sessions/{session_id}")

    # ── JobManager REST — job control ─────────────────────────────────────

    async def list_jobs(self) -> list[dict]:
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            resp = await c.get(f"{self._jm}/v1/jobs")
            resp.raise_for_status()
            return resp.json().get("jobs", [])

    async def find_jobs_by_name(self, name: str) -> list[dict]:
        """Jobs whose name is EXACTLY `name` — used only with the unique,
        per-deployment pipeline name this runner sets before submitting
        (FlinkDeployment.submission_tag), never with a guessable prefix.
        Raises on a JobManager error (caller treats that as "unknown")."""
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            resp = await c.get(f"{self._jm}/v1/jobs/overview")
            resp.raise_for_status()
            return [j for j in resp.json().get("jobs", []) if j.get("name") == name]

    async def get_job(self, flink_job_id: str) -> dict:
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            resp = await c.get(f"{self._jm}/v1/jobs/{flink_job_id}")
            resp.raise_for_status()
            return resp.json()

    async def job_state(self, flink_job_id: str) -> str:
        """Live Flink state, or "NOT_FOUND" (JobManager answered 404), or
        "UNKNOWN" (JobManager unreachable / errored). RUN-FLINK-013:
        callers must never treat UNKNOWN as terminal."""
        try:
            detail = await self.get_job(flink_job_id)
        except httpx.HTTPStatusError as e:
            if e.response is not None and e.response.status_code == 404:
                return "NOT_FOUND"
            return "UNKNOWN"
        except Exception:  # noqa: BLE001
            return "UNKNOWN"
        return (detail or {}).get("state") or "UNKNOWN"

    async def cancel_job(self, flink_job_id: str) -> None:
        """Force-cancel a Flink job (no savepoint)."""
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            resp = await c.patch(f"{self._jm}/v1/jobs/{flink_job_id}", params={"mode": "cancel"})
            resp.raise_for_status()

    async def stop_with_savepoint(
        self, flink_job_id: str, savepoint_dir: str, drain: bool = True, *, on_trigger=None,
    ) -> str:
        """Stop with savepoint (clean shutdown). Returns the savepoint path.
        on_trigger(trigger_id) is awaited as soon as Flink accepts the stop,
        so a restarted process can look the savepoint up (RUN-FLINK-013)."""
        async with httpx.AsyncClient(timeout=self._submit_timeout) as c:
            resp = await c.post(
                f"{self._jm}/v1/jobs/{flink_job_id}/stop",
                json={"drain": drain, "targetDirectory": savepoint_dir},
            )
            resp.raise_for_status()
            trigger_id = resp.json()["request-id"]
            if on_trigger is not None:
                await on_trigger(trigger_id)
            for _ in range(120):
                st = await c.get(
                    f"{self._jm}/v1/jobs/{flink_job_id}/savepoints/{trigger_id}",
                )
                body = st.json()
                op_status = body.get("status", {}).get("id", "IN_PROGRESS")
                if op_status == "COMPLETED":
                    operation = body.get("operation") or {}
                    if operation.get("failure-cause"):
                        raise RuntimeError(f"Savepoint failed: {operation['failure-cause']}")
                    return operation["location"]
                if op_status == "FAILED":
                    raise RuntimeError(f"Savepoint failed: {body}")
                await asyncio.sleep(1)
        raise TimeoutError("Savepoint creation timed out")

    async def get_savepoint_status(self, flink_job_id: str, trigger_id: str) -> dict:
        """{"status": COMPLETED|IN_PROGRESS|FAILED|UNKNOWN, "location": str|None}."""
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as c:
                resp = await c.get(f"{self._jm}/v1/jobs/{flink_job_id}/savepoints/{trigger_id}")
                if resp.status_code != 200:
                    return {"status": "UNKNOWN", "location": None}
                body = resp.json()
        except Exception:  # noqa: BLE001
            return {"status": "UNKNOWN", "location": None}
        op_status = body.get("status", {}).get("id", "UNKNOWN")
        operation = body.get("operation") or {}
        if op_status == "COMPLETED" and operation.get("failure-cause"):
            return {"status": "FAILED", "location": None}
        return {"status": op_status, "location": operation.get("location")}

    # ── JobManager REST — metrics ─────────────────────────────────────────

    async def get_job_metrics(self, flink_job_id: str) -> dict:
        """Fetch throughput, latency, and resource metrics from the JobManager."""
        metric_ids = [
            "numRecordsInPerSecond", "numRecordsOutPerSecond",
            "numBytesInPerSecond", "numBytesOutPerSecond",
            "numRestarts", "uptime",
        ]
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            resp = await c.get(
                f"{self._jm}/v1/jobs/{flink_job_id}/metrics",
                params={"get": ",".join(metric_ids)},
            )
            resp.raise_for_status()
            raw = {m["id"]: m.get("value") for m in resp.json()}

            # Vertex-level backpressure
            job_detail = await c.get(f"{self._jm}/v1/jobs/{flink_job_id}")
            vertices = job_detail.json().get("vertices", [])
            backpressure = {}
            input_watermarks: list[int] = []
            for v in vertices:
                vid = v["id"]
                bp_resp = await c.get(
                    f"{self._jm}/v1/jobs/{flink_job_id}/vertices/{vid}/backpressure",
                )
                if bp_resp.status_code == 200:
                    bp = bp_resp.json()
                    backpressure[v.get("name", vid)] = _backpressure_ratio(bp)
                # RUN-FLINK-015: per-subtask currentInputWatermark — the job's
                # event-time progress is its slowest operator's watermark.
                try:
                    wm_resp = await c.get(
                        f"{self._jm}/v1/jobs/{flink_job_id}/vertices/{vid}/watermarks",
                    )
                    if wm_resp.status_code == 200:
                        for m in wm_resp.json() or []:
                            if str(m.get("id", "")).endswith("currentInputWatermark"):
                                wm = _int(m.get("value"))
                                if wm is not None and wm > 0 and wm != _NO_WATERMARK:
                                    input_watermarks.append(wm)
                except httpx.HTTPError:
                    pass

            # TaskManager heap
            tm_resp = await c.get(f"{self._jm}/v1/taskmanagers")
            tms = tm_resp.json().get("taskmanagers", [])
            heap_used = sum(tm.get("metrics", {}).get("heapUsed", 0) for tm in tms)
            heap_max = sum(tm.get("metrics", {}).get("heapMax", 0) for tm in tms)

        return {
            "records_in_per_sec": _float(raw.get("numRecordsInPerSecond")),
            "records_out_per_sec": _float(raw.get("numRecordsOutPerSecond")),
            "bytes_in_per_sec": _float(raw.get("numBytesInPerSecond")),
            "bytes_out_per_sec": _float(raw.get("numBytesOutPerSecond")),
            "num_restarts": _int(raw.get("numRestarts")),
            "backpressure": backpressure,
            "heap_used_bytes": heap_used,
            "heap_max_bytes": heap_max,
            # None (not 0) when no operator has emitted a watermark yet.
            "watermark_lag_ms": (
                max(0, int(time.time() * 1000) - min(input_watermarks)) if input_watermarks else None
            ),
        }

    async def get_checkpoint_info(self, flink_job_id: str) -> dict:
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            resp = await c.get(f"{self._jm}/v1/jobs/{flink_job_id}/checkpoints")
            resp.raise_for_status()
            data = resp.json()
            counts = data.get("counts", {})
            latest = data.get("latest", {})
            completed = latest.get("completed") or {}
            failed = latest.get("failed") or {}
            # FAILED when the most recent checkpoint attempt failed (its
            # failure is newer than the latest completion).
            completed_ts = completed.get("latest_ack_timestamp") or completed.get("trigger_timestamp") or 0
            failed_ts = failed.get("failure_timestamp") or failed.get("trigger_timestamp") or 0
            if failed and failed_ts > completed_ts:
                cp_status = "FAILED"
            elif completed:
                cp_status = "COMPLETED"
            else:
                cp_status = None
            return {
                "id": completed.get("id"),
                "status": cp_status,
                "size_bytes": completed.get("state_size"),
                "duration_ms": completed.get("end_to_end_duration"),
                "savepoint_path": completed.get("external_path"),
                "completed": counts.get("completed", 0),
                "failed": counts.get("failed", 0),
                "in_progress": counts.get("in_progress", 0),
            }

    async def trigger_savepoint(self, flink_job_id: str, savepoint_dir: str) -> str:
        """Trigger a savepoint without stopping the job."""
        async with httpx.AsyncClient(timeout=self._submit_timeout) as c:
            resp = await c.post(
                f"{self._jm}/v1/jobs/{flink_job_id}/savepoints",
                json={"targetDirectory": savepoint_dir, "cancel-job": False},
            )
            resp.raise_for_status()
            trigger_id = resp.json()["request-id"]
            for _ in range(60):
                st = await c.get(
                    f"{self._jm}/v1/jobs/{flink_job_id}/savepoints/{trigger_id}",
                )
                op_status = st.json().get("status", {}).get("id", "IN_PROGRESS")
                if op_status == "COMPLETED":
                    return st.json()["operation"]["location"]
                if op_status == "FAILED":
                    raise RuntimeError(f"Savepoint failed: {st.json()}")
                await asyncio.sleep(1)
        raise TimeoutError("Savepoint timed out")


def _backpressure_ratio(bp: dict) -> float:
    """Flink's /backpressure reports backpressureLevel as a string
    ("ok"/"low"/"high") plus per-subtask ratios — take the max ratio."""
    subtasks = bp.get("subtasks") or []
    ratios = [_float(st.get("ratio")) for st in subtasks]
    ratios = [r for r in ratios if r is not None]
    if ratios:
        return max(ratios)
    level = bp.get("backpressureLevel")
    if isinstance(level, (int, float)):
        return float(level)
    return {"ok": 0.0, "low": 0.5, "high": 1.0}.get(str(level).lower(), 0.0)


def _float(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _int(v: Any) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


_client: FlinkGatewayClient | None = None


def get_flink_client() -> FlinkGatewayClient:
    global _client
    if _client is None:
        _client = FlinkGatewayClient()
    return _client
