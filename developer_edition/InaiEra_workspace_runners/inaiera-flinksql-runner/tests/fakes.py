"""Shared test doubles for the Flink Gateway/JobManager client."""
from __future__ import annotations

import re

from app.executors.flink_gateway import JobIdUnavailableError

_PIPELINE_NAME_RE = re.compile(r"SET\s+'pipeline\.name'\s*=\s*'([^']+)'", re.IGNORECASE)


class ProcessCrash(BaseException):
    """Simulates the process dying mid-operation: a BaseException, so none
    of the application's `except Exception` recovery paths run."""


class FakeFlinkClient:
    """In-memory Flink. `job_states` maps flink job id -> state; jobs are
    also indexed by their pipeline.name (the deployment's submission_tag).

    submit_mode: ok | no_job_id | crash_after_submit | error_before_submit
    stop_mode:   ok | fail_job_still_running | fail_unknown | fail_job_failed
    """

    def __init__(self, *, job_states=None, submit_mode="ok", stop_mode="ok"):
        self.job_states: dict[str, str] = dict(job_states or {})
        self.jobs_by_name: dict[str, list[dict]] = {}
        self.submit_mode = submit_mode
        self.stop_mode = stop_mode
        self.unknown = False             # JobManager unreachable
        self.find_raises = False
        self.cancel_raises = False
        self.op_status: str | None = None
        self.savepoint_status: dict | None = None
        self.execute_statements_calls = 0
        self.stop_with_savepoint_calls = 0
        self.submitted_sql: list[str] = []
        self.cancelled: list[str] = []
        self.list_jobs_called = False

    async def create_session(self, session_name=None):
        return "sess-1"

    async def close_session(self, session_id):
        pass

    async def execute_statements(self, session_id, sql, on_submit=None):
        self.execute_statements_calls += 1
        n = self.execute_statements_calls
        self.submitted_sql.append(sql)
        if self.submit_mode == "error_before_submit":
            raise RuntimeError("gateway rejected the statement")
        op = f"op-{n}"
        if on_submit is not None:
            await on_submit(op, "INSERT ...")
        job_id = f"job-{n}"
        self.job_states[job_id] = "RUNNING"
        m = _PIPELINE_NAME_RE.search(sql)
        if m:
            self.jobs_by_name.setdefault(m.group(1), []).append(
                {"jid": job_id, "name": m.group(1), "state": "RUNNING", "start-time": n})
        if self.submit_mode == "no_job_id":
            raise JobIdUnavailableError("no job id", session_id=session_id, op_handle=op,
                                        statement="INSERT ...")
        if self.submit_mode == "crash_after_submit":
            raise ProcessCrash("process died right after the Gateway accepted the job")
        return [
            {"op_handle": "op-ddl", "state": "FINISHED", "statement": "SET ...", "kind": "SET",
             "flink_job_id": None},
            {"op_handle": op, "state": "FINISHED", "statement": "INSERT ...", "kind": "INSERT",
             "flink_job_id": job_id},
        ]

    def _set_state(self, job_id, state):
        self.job_states[job_id] = state
        for jobs in self.jobs_by_name.values():
            for j in jobs:
                if j["jid"] == job_id:
                    j["state"] = state

    async def get_job(self, flink_job_id):
        if self.unknown:
            raise RuntimeError("connection refused")
        return {"state": self.job_states.get(flink_job_id, "RUNNING")}

    async def job_state(self, flink_job_id):
        if self.unknown:
            return "UNKNOWN"
        return self.job_states.get(flink_job_id, "NOT_FOUND")

    async def find_jobs_by_name(self, name):
        if self.unknown or self.find_raises:
            raise RuntimeError("jobmanager unreachable")
        return [dict(j) for j in self.jobs_by_name.get(name, [])]

    async def cancel_job(self, flink_job_id):
        if self.cancel_raises:
            raise RuntimeError("cancel rejected")
        self.cancelled.append(flink_job_id)
        self._set_state(flink_job_id, "CANCELED")

    async def stop_with_savepoint(self, flink_job_id, savepoint_dir, drain=True, on_trigger=None):
        self.stop_with_savepoint_calls += 1
        n = self.stop_with_savepoint_calls
        if on_trigger is not None:
            await on_trigger(f"trigger-{n}")
        if self.stop_mode == "fail_job_still_running":
            raise RuntimeError("savepoint failed: checkpoint declined")
        if self.stop_mode == "fail_unknown":
            self.unknown = True
            raise RuntimeError("connection reset by peer")
        if self.stop_mode == "fail_job_failed":
            self._set_state(flink_job_id, "FAILED")
            raise RuntimeError("job failed during savepoint")
        self._set_state(flink_job_id, "FINISHED")
        return f"{savepoint_dir}/savepoint-{n}"

    async def get_savepoint_status(self, flink_job_id, trigger_id):
        return self.savepoint_status or {"status": "UNKNOWN", "location": None}

    async def get_operation_status(self, session_id, op_handle):
        return self.op_status

    async def list_jobs(self):
        self.list_jobs_called = True
        raise AssertionError("list_jobs() must never be used to identify a deployment's job")


def patch_flink_client(monkeypatch, fake):
    monkeypatch.setattr("app.services.job_service.get_flink_client", lambda: fake)
    monkeypatch.setattr("app.services.deployment_reconciler.get_flink_client", lambda: fake)


async def leader_tick() -> None:
    """One pass of the leader's reconciler loop over 'deploying'/'pausing' jobs (FL-011): deploy()/stop() only PERSIST the work, this is
    what starts it. Starts the work in the background, like the real loop."""
    from app.services import deployment_reconciler
    await deployment_reconciler._reconcile_work(wait=False)
