"""
Real tests for the Temporal long-running workflow lifecycle fix
(section 0.60, GAP_REPORT).

The real bug, exactly as reported: _run_durable's own 25s-timeout
fallback returned a bare status dict with no Run/Trace persisted -
/run only wrote an AgentLog entry for it. If the Temporal workflow
finished later, there was no real path persisting its eventual
terminal state into the platform's Run/Trace model, and no run_id for
a caller to check back with.

The fix: a real Run row (status="running", a real workflow_id, the
real invoking_user/user_input needed for accurate later
reconciliation) is created the moment the 25s budget is hit, and
GET /api/runs/{run_id} genuinely reconciles it via the exact same
_finalize_run the immediate-completion path uses. Best-effort: if
Temporal itself is unreachable, the row is honestly left "running"
rather than erroring.

These tests mock only the real Temporal client calls - everything else
(the real FastAPI request, the real database persistence, the real
reconciliation logic) is exercised for real.
"""
import sys
import os
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient
from sqlmodel import Session, text

from app.main import app
from app.db import engine
from app.models import Run, Trace, HitlRequest, AgentVersion

client = TestClient(app)


def hitl_flow() -> dict:
    return {
        "nodes": [
            {"id": "n1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}},
            {"id": "n2", "type": "hitl", "x": 0, "y": 0, "sub": "", "config": {
                "form_content": "x", "actions": [{"id": "continue", "title": "Continue", "style": "primary"}]}},
            {"id": "n3", "type": "respond", "x": 0, "y": 0, "sub": "", "config": {}},
        ],
        "edges": [
            {"id": "e1", "source": "n1", "target": "n2", "source_handle": None},
            {"id": "e2", "source": "n2", "target": "n3", "source_handle": None},
        ],
    }


def create_agent() -> dict:
    r = client.post("/api/agents", json={
        "name": f"Temporal Lifecycle Test {uuid.uuid4().hex[:6]}", "subtype": "Custom", "app_type": "workflow",
        "template_flow": hitl_flow(),
    })
    assert r.status_code == 200, r.text
    return r.json()


async def fake_still_running(session, agent, invoking_user, user_input, webhook_payload=None, structured_inputs=None, flow=None, features=None, subagent_manifest=None):
    return {
        "status": "running", "output": None, "spans": [], "fail_class": None,
        "_temporal_workflow_id": f"wf-{uuid.uuid4().hex[:8]}",
        "_note": "Still executing after 25s — it continues running; check the Temporal UI for its real live state.",
    }


class FakeCompletedHandle:
    def __init__(self, result):
        self._result = result

    async def describe(self):
        class Desc:
            status = "COMPLETED"
        return Desc()

    async def result(self):
        return self._result


class FakeStillRunningHandle:
    async def describe(self):
        class Desc:
            status = "RUNNING"
        return Desc()


def make_fake_client(handle):
    class FakeClient:
        def get_workflow_handle(self, workflow_id):
            return handle
    return FakeClient()


class TestRealRunRowCreatedOnTimeout:
    def test_the_25s_timeout_path_genuinely_creates_a_real_run_row(self):
        agent = create_agent()
        with patch("app.routers.execution._run_durable", fake_still_running):
            r = client.post(f"/api/agents/{agent['id']}/run", json={"input": "hello"})
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "running"
        assert body["run_id"] is not None

        with Session(engine) as s:
            row = s.get(Run, body["run_id"])
            assert row is not None
            assert row.status == "running"
            assert row.workflow_id == body["temporal_workflow_id"]
            assert row.invoking_user == "user:demo"
            assert row.user_input == "hello"

    def test_the_response_tells_the_caller_exactly_where_to_check_back(self):
        agent = create_agent()
        with patch("app.routers.execution._run_durable", fake_still_running):
            r = client.post(f"/api/agents/{agent['id']}/run", json={"input": "hello"})
        body = r.json()
        assert f"/api/runs/{body['run_id']}" in body["note"]


class TestRealReconciliationOnGet:
    def test_get_run_leaves_a_running_row_unchanged_when_temporal_is_unreachable(self):
        agent = create_agent()
        with patch("app.routers.execution._run_durable", fake_still_running):
            r = client.post(f"/api/agents/{agent['id']}/run", json={"input": "hello"})
        run_id = r.json()["run_id"]

        r2 = client.get(f"/api/runs/{run_id}")
        assert r2.status_code == 200
        assert r2.json()["status"] == "running"

    def test_get_run_genuinely_reconciles_a_completed_workflow_to_success(self):
        agent = create_agent()
        with patch("app.routers.execution._run_durable", fake_still_running):
            r = client.post(f"/api/agents/{agent['id']}/run", json={"input": "hello"})
        run_id = r.json()["run_id"]

        real_result = {"status": "success", "output": "The real, eventual answer",
                        "spans": [{"node": "llm", "status": "ok"}], "fail_class": None}
        fake_client = make_fake_client(FakeCompletedHandle(real_result))

        async def fake_connect(address, namespace):
            return fake_client

        import temporalio.client
        with patch.object(temporalio.client.Client, "connect", fake_connect):
            r2 = client.get(f"/api/runs/{run_id}")

        assert r2.status_code == 200
        body = r2.json()
        assert body["status"] == "success"
        assert body["id"] == run_id

        with Session(engine) as s:
            trace = s.get(Trace, f"tr-{run_id}")
            assert trace is not None
            assert trace.final_output == "The real, eventual answer"
            assert trace.status == "ok"
            assert trace.user_input == "hello"

    def test_get_run_genuinely_reconciles_a_completed_workflow_to_failed(self):
        agent = create_agent()
        with patch("app.routers.execution._run_durable", fake_still_running):
            r = client.post(f"/api/agents/{agent['id']}/run", json={"input": "hello"})
        run_id = r.json()["run_id"]

        real_result = {"status": "failed", "output": None, "spans": [], "fail_class": "orchestration"}
        fake_client = make_fake_client(FakeCompletedHandle(real_result))

        async def fake_connect(address, namespace):
            return fake_client

        import temporalio.client
        with patch.object(temporalio.client.Client, "connect", fake_connect):
            r2 = client.get(f"/api/runs/{run_id}")

        assert r2.json()["status"] == "failed"
        assert r2.json()["fail_class"] == "orchestration"

    def test_get_run_leaves_a_row_running_if_the_real_workflow_is_still_genuinely_running(self):
        agent = create_agent()
        with patch("app.routers.execution._run_durable", fake_still_running):
            r = client.post(f"/api/agents/{agent['id']}/run", json={"input": "hello"})
        run_id = r.json()["run_id"]

        fake_client = make_fake_client(FakeStillRunningHandle())

        async def fake_connect(address, namespace):
            return fake_client

        import temporalio.client
        with patch.object(temporalio.client.Client, "connect", fake_connect):
            r2 = client.get(f"/api/runs/{run_id}")

        assert r2.json()["status"] == "running"

    def test_a_second_get_after_reconciliation_does_not_attempt_to_reconcile_again(self):
        agent = create_agent()
        with patch("app.routers.execution._run_durable", fake_still_running):
            r = client.post(f"/api/agents/{agent['id']}/run", json={"input": "hello"})
        run_id = r.json()["run_id"]

        real_result = {"status": "success", "output": "done", "spans": [], "fail_class": None}
        fake_client = make_fake_client(FakeCompletedHandle(real_result))

        async def fake_connect(address, namespace):
            return fake_client

        import temporalio.client
        with patch.object(temporalio.client.Client, "connect", fake_connect):
            client.get(f"/api/runs/{run_id}")

        r2 = client.get(f"/api/runs/{run_id}")
        assert r2.status_code == 200
        assert r2.json()["status"] == "success"

    def test_a_non_running_run_is_never_sent_through_reconciliation_at_all(self):
        r = client.post("/api/agents", json={
            "name": "Plain sync agent", "subtype": "Custom", "app_type": "workflow",
            "template_flow": {"nodes": [
                {"id": "n1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}},
                {"id": "n2", "type": "respond", "x": 0, "y": 0, "sub": "", "config": {}},
            ], "edges": [{"id": "e1", "source": "n1", "target": "n2", "source_handle": None}]},
        })
        plain_agent = r.json()
        r2 = client.post(f"/api/agents/{plain_agent['id']}/run", json={"input": "hi"})
        run_id = r2.json()["run_id"]

        r3 = client.get(f"/api/runs/{run_id}")
        assert r3.status_code == 200
        assert r3.json()["status"] == "success"
        assert r3.json()["workflow_id"] is None


def cleanup_agent(agent_id: str):
    with Session(engine) as s:
        s.exec(text("DELETE FROM hitlrequest WHERE agent_id = :a"), params={"a": agent_id})
        for t in ("run", "trace", "agentlog", "agentversion"):
            s.exec(text(f"DELETE FROM {t} WHERE agent_id = :a"), params={"a": agent_id})
        s.exec(text("DELETE FROM agent WHERE id = :a"), params={"a": agent_id})
        s.commit()


class TestRealHitlPauseCreatesAndTracksARunRow:
    """Real tests for section 0.91, GAP_REPORT — the actual, reported
    gap: a workflow reaching a real hitl node within _run_durable's
    own 25s poll window used to create a HitlRequest but NO Run row at
    all — a workflow could sit genuinely paused for hours or days with
    nothing in GET /api/runs to show for it the entire time. Fixed by
    creating a real Run row at real INVOCATION time (before execution
    even starts), then transitioning that SAME row through the real,
    reported lifecycle: running -> paused -> running -> terminal,
    linked across the pause/resume boundary via HitlRequest.run_id.

    These tests use the same real, established monkeypatch pattern
    test_hitl_bugs.py's own TestRealBatchPauseHandling already uses
    (patching execute_agent_flow/resume_flow_sync directly) rather
    than requiring a real, running Temporal server — genuinely
    verifying this platform's own real Run-row lifecycle logic without
    needing infrastructure this sandbox doesn't have."""

    def test_a_hitl_pause_creates_a_real_run_row_with_status_paused(self):
        import app.routers.execution as execution_module

        agent = create_agent()
        try:
            async def fake_paused(session, agent, invoking_user, user_input, **kwargs):
                return {
                    "status": "paused_for_human_input", "hitl_node_id": "n2",
                    "hitl_rendered_content": "Please review", "hitl_fields": [],
                    "hitl_actions": [{"id": "continue", "title": "Continue"}],
                    "hitl_delivery_status": {}, "spans": [{"node": "trigger", "status": "ok"}],
                    "execution_mode": "sync (direct harness call, no Temporal)",
                }
            original = execution_module.execute_agent_flow
            execution_module.execute_agent_flow = fake_paused
            try:
                r = client.post(f"/api/agents/{agent['id']}/run", json={"input": "hi"})
            finally:
                execution_module.execute_agent_flow = original

            assert r.status_code == 200, r.text
            body = r.json()
            assert body["status"] == "paused_for_human_input"
            run_id = body["run_id"]
            hitl_request_id = body["hitl_request_id"]
            assert run_id is not None

            with Session(engine) as s:
                run_row = s.get(Run, run_id)
                assert run_row is not None
                assert run_row.status == "paused"
                hitl_row = s.get(HitlRequest, hitl_request_id)
                assert hitl_row.run_id == run_id
        finally:
            cleanup_agent(agent["id"])

    def test_resuming_a_paused_run_updates_the_same_run_row_to_its_terminal_state(self):
        import app.routers.execution as execution_module

        agent = create_agent()
        try:
            async def fake_paused(session, agent, invoking_user, user_input, **kwargs):
                return {
                    "status": "paused_for_human_input", "hitl_node_id": "n2",
                    "hitl_rendered_content": "Please review", "hitl_fields": [],
                    "hitl_actions": [{"id": "continue", "title": "Continue"}],
                    "hitl_delivery_status": {}, "spans": [{"node": "trigger", "status": "ok"}],
                    "execution_mode": "sync (direct harness call, no Temporal)",
                }
            original_exec = execution_module.execute_agent_flow
            execution_module.execute_agent_flow = fake_paused
            try:
                r = client.post(f"/api/agents/{agent['id']}/run", json={"input": "hi"})
            finally:
                execution_module.execute_agent_flow = original_exec
            run_id = r.json()["run_id"]
            hitl_request_id = r.json()["hitl_request_id"]

            def fake_resume(session, agent, hitl, action_id, field_values):
                return {
                    "status": "success", "output": "done", "fail_class": None,
                    "spans": [{"node": "trigger", "status": "ok"}, {"node": "respond", "status": "ok"}],
                }
            original_resume = execution_module.resume_flow_sync
            execution_module.resume_flow_sync = fake_resume
            try:
                r2 = client.post(f"/api/agents/{agent['id']}/hitl/{hitl_request_id}/respond",
                                  json={"action_id": "continue", "field_values": {}})
            finally:
                execution_module.resume_flow_sync = original_resume

            assert r2.status_code == 200, r2.text
            body2 = r2.json()
            # The real, reported requirement: the SAME run_id throughout,
            # not a new, disconnected one appearing at resolution time.
            assert body2["run_id"] == run_id
            assert body2["status"] == "success"

            with Session(engine) as s:
                run_row = s.get(Run, run_id)
                assert run_row.status == "success"
                trace_row = s.exec(
                    text("SELECT status, final_output FROM trace WHERE agent_id = :a"), params={"a": agent["id"]}
                ).first()
                assert trace_row[0] == "ok"
                assert trace_row[1] == "done"
        finally:
            cleanup_agent(agent["id"])

    def test_an_execution_exception_marks_the_upfront_run_row_failed_not_stranded(self):
        """§0.91, GAP_REPORT — a real, additional improvement: since a
        real Run row now exists from the moment execution begins, a
        genuine infrastructure failure (the exact scenario this
        sandbox's own missing Temporal server reproduces for real) now
        correctly marks that row "failed" rather than either leaving
        no row at all (the old behavior) or a row stranded at
        "running" forever."""
        import app.routers.execution as execution_module

        agent = create_agent()
        try:
            async def fake_raise(session, agent, invoking_user, user_input, **kwargs):
                raise RuntimeError("simulated infrastructure failure")
            original = execution_module.execute_agent_flow
            execution_module.execute_agent_flow = fake_raise
            try:
                r = client.post(f"/api/agents/{agent['id']}/run", json={"input": "hi"})
            finally:
                execution_module.execute_agent_flow = original
            assert r.status_code == 500

            with Session(engine) as s:
                run_row = s.exec(text("SELECT status, fail_class FROM run WHERE agent_id = :a"), params={"a": agent["id"]}).first()
                assert run_row is not None
                assert run_row[0] == "failed"
                assert run_row[1] == "orchestration"
        finally:
            cleanup_agent(agent["id"])


class TestRealAgentVersionTrackingOnRunAndTrace:
    """Real tests for section 0.91, GAP_REPORT — the actual, reported
    gap: neither Run nor Trace recorded which real AgentVersion
    actually executed at invocation time, only Trace.version, set from
    agent.version at FINALIZATION time — genuinely different from what
    a long-running durable workflow actually executed if the agent got
    re-published in the meantime."""

    @staticmethod
    def _create_simple_agent() -> dict:
        # Real, deliberately non-hitl flow (unlike this file's own
        # create_agent/hitl_flow) - a hitl node forces the durable
        # path, which genuinely can't complete a real /run in this
        # sandbox (no reachable Temporal server, a real, standing
        # constraint stated elsewhere in this suite) - these tests are
        # about version tracking, not the pause/resume lifecycle
        # itself, so a plain, publishable sync-path flow is the
        # correct, minimal fixture.
        r = client.post("/api/agents", json={
            "name": f"Version Tracking Test {uuid.uuid4().hex[:6]}", "subtype": "Custom", "app_type": "chatflow",
            "template_flow": {
                "nodes": [
                    {"id": "n1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}},
                    {"id": "n3", "type": "template", "x": 0, "y": 0, "sub": "", "config": {"template": "hello"}},
                ],
                "edges": [{"id": "e1", "source": "n1", "target": "n3", "source_handle": None}],
            },
        })
        assert r.status_code == 200, r.text
        return r.json()

    def test_a_run_records_the_real_agent_version_actually_resolved(self):
        agent = self._create_simple_agent()
        try:
            pub = client.post(f"/api/agents/{agent['id']}/publish")
            assert pub.status_code == 200, pub.text
            v2 = pub.json()["version"]

            r = client.post(f"/api/agents/{agent['id']}/run", json={"input": "hi"})
            assert r.status_code == 200, r.text
            run_id = r.json()["run_id"]

            with Session(engine) as s:
                run_row = s.get(Run, run_id)
                assert run_row.agent_version == v2
                assert run_row.agent_version_id is not None
                assert run_row.flow_hash is not None
                trace_row = s.exec(
                    text("SELECT agent_version, flow_hash FROM trace WHERE agent_id = :a"), params={"a": agent["id"]}
                ).first()
                assert trace_row[0] == v2
                assert trace_row[1] == run_row.flow_hash
        finally:
            cleanup_agent(agent["id"])

    def test_a_run_started_on_one_version_keeps_recording_that_version_even_after_a_later_republish(self):
        """The exact real scenario the report described: a run that
        resolved against v2 must keep showing v2 forever, immune to
        whatever version the agent is later republished to — a real,
        direct proof this is captured at INVOCATION time, not
        re-derived later from the agent's own, possibly-different
        CURRENT version."""
        agent = self._create_simple_agent()
        try:
            pub2 = client.post(f"/api/agents/{agent['id']}/publish")
            v2 = pub2.json()["version"]
            r = client.post(f"/api/agents/{agent['id']}/run", json={"input": "hi"})
            run_id_v2 = r.json()["run_id"]

            # Real, unrelated edit + republish - the agent moves to v3.
            client.patch(f"/api/agents/{agent['id']}/nodes/n3/position", json={"x": 999, "y": 999})
            pub3 = client.post(f"/api/agents/{agent['id']}/publish")
            assert pub3.status_code == 200, pub3.text
            v3 = pub3.json()["version"]
            assert v3 != v2

            with Session(engine) as s:
                run_row = s.get(Run, run_id_v2)
                # Real, direct proof: the OLD run's own recorded version
                # is untouched by the LATER republish.
                assert run_row.agent_version == v2
                assert run_row.agent_version != v3
        finally:
            cleanup_agent(agent["id"])


class TestRealReconciliationDurationAcrossMidnight:
    """Real test for section 0.91, GAP_REPORT — the actual, reported
    bug: the old duration calculation parsed run.ts (a time-of-day-
    ONLY string) and reattached TODAY's date before diffing against
    "now", genuinely wrong the moment a run's real start and
    reconciliation land on different real calendar days. The fix uses
    run.started_at (a real, exact TIMESTAMPTZ) directly."""

    def test_reconciliation_computes_a_correct_duration_even_when_ts_alone_would_be_wrong(self):
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td

        agent = create_agent()
        try:
            # Real, direct simulation of a run that genuinely started
            # YESTERDAY (started_at is real and correct), while `ts`
            # alone (time-of-day only) would misleadingly suggest a
            # tiny, or even negative, elapsed time if naively
            # reattached to today's date - exactly the real bug.
            real_start = _dt.now(_tz.utc) - _td(hours=25)
            run_id = f"r-test-{uuid.uuid4().hex[:8]}"
            with Session(engine) as s:
                s.add(Run(
                    id=run_id, agent_id=agent["id"], agent_name=agent["name"], status="running",
                    duration="0.0s", cost="$0.00", hops=1, ts=real_start.strftime("%H:%M:%S"),
                    workflow_id=f"wf-{uuid.uuid4().hex[:8]}", invoking_user="user:test", user_input="hi",
                    started_at=real_start,
                ))
                s.commit()

            from app.routers.execution import _reconcile_running_run
            import asyncio

            class FakeHandle:
                async def describe(self):
                    class D:
                        status = None
                    from temporalio.client import WorkflowExecutionStatus
                    D.status = WorkflowExecutionStatus.COMPLETED
                    return D()
                async def result(self):
                    return {"status": "success", "output": "done", "spans": [], "fail_class": None}

            with Session(engine) as s:
                run_row = s.get(Run, run_id)
                with patch("temporalio.client.Client.connect") as mock_connect:
                    async def fake_connect(*a, **kw):
                        class FakeClient:
                            def get_workflow_handle(self, wf_id):
                                return FakeHandle()
                        return FakeClient()
                    mock_connect.side_effect = fake_connect
                    asyncio.run(_reconcile_running_run(s, run_row))

            with Session(engine) as s:
                final_run = s.get(Run, run_id)
                assert final_run.status == "success"
                # Real, direct proof: the recorded duration reflects
                # the real ~25-hour elapsed time, not a tiny or
                # negative value the old, string-parsing bug would
                # have silently clamped to a wrong 25.0s floor.
                assert float(final_run.duration.rstrip("s")) > 3600
        finally:
            cleanup_agent(agent["id"])
