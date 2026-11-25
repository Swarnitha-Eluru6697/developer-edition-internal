"""An http node that was refused still reported a green span.

`handle_http` signals failure by writing `{"error": ...}` into
`ctx["vars"]["http_output"]` and returning normally, rather than
raising. The span builder in `_execute_flow` sets `status: "ok"` for
any handler that returns, so a node that made no request at all was
recorded as having succeeded.

Found on Showcase 5 — Connectors, whose http node is configured for
`http://localhost:8000/api/health`. The egress policy blocks loopback,
correctly, so the call never happens. The trace showed:

    {"node": "http", "status": "ok", "duration_ms": 0}

0 ms for a network call is the only tell, and it is not one anybody
should have to notice. The run's own verdict is deliberately unchanged
— the handler chose to return, downstream nodes really did run — but
the span now says what happened.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from unittest.mock import patch

from app.egress import SSRFBlocked


def _flow(url="https://example.com/thing"):
    """Trigger -> http -> template -> respond.

    The template reads http_output so a failure is observable in the
    output as well as the span, which is how this defect presented:
    an empty string where a body should have been.
    """
    return {
        "nodes": [
            {"id": "n1", "type": "trigger", "x": 0, "y": 0,
             "config": {"trigger_type": "user_input", "fields": []}},
            {"id": "n2", "type": "http", "x": 100, "y": 0,
             "config": {"url": url, "method": "GET", "body_type": "none", "timeout_s": 5}},
            {"id": "n3", "type": "template", "x": 200, "y": 0,
             "config": {"template": "HTTP {{http_status}}: {{http_body}}"}},
            {"id": "n4", "type": "respond", "x": 300, "y": 0, "config": {"format": "text"}},
        ],
        "edges": [
            {"id": "e1", "source": "n1", "target": "n2"},
            {"id": "e2", "source": "n2", "target": "n3"},
            {"id": "e3", "source": "n3", "target": "n4"},
        ],
    }


def _run(flow):
    """Execute the flow and return its http span.

    Goes through run_flow_sync rather than calling handle_http
    directly: the defect was in the span the EXECUTOR builds around the
    handler, so a handler-level test would pass while the bug stood.

    The agent is a plain object, never persisted. This suite runs
    against the live database, and a test that inserts an agent row to
    check a span is a test that leaves debris behind — roughly 500 junk
    agents got here that way once already.
    """
    from sqlmodel import Session

    from app.db import engine
    from app.harness import run_flow_sync

    class _Agent:
        id = "a-test-http-span"
        name = "http span test"
        flow = None
        features = {}
        subtype = "Workflow"
        app_type = "workflow"
        status = "draft"
        version = "v1"
        description = ""
        plane = "App"
        type = "App"

    agent = _Agent()
    agent.flow = flow
    with Session(engine) as session:
        result = run_flow_sync(session, agent, "user:test", "go", flow=flow)
    spans = result.get("spans", [])
    http_spans = [s for s in spans if s.get("node") == "http"]
    assert http_spans, f"no http span was recorded; got {[s.get('node') for s in spans]}"
    return result, http_spans[0]


class TestABlockedCallIsNotASuccess:
    def test_an_egress_block_marks_the_span_failed(self):
        with patch("app.egress.safe_http_request",
                   side_effect=SSRFBlocked("'localhost' is a blocked hostname")):
            _result, span = _run(_flow("http://localhost:8000/api/health"))
        assert span["status"] == "error", \
            "a node that never made a request was recorded as ok"
        assert "blocked" in span["error"]
        assert "localhost" in span["error"]

    def test_the_span_says_the_flow_carried_on(self):
        # Without this the reader has to guess whether an errored span
        # aborted the run. It did not, and that is deliberate.
        with patch("app.egress.safe_http_request",
                   side_effect=SSRFBlocked("'localhost' is a blocked hostname")):
            result, span = _run(_flow("http://localhost:8000/api/health"))
        assert span["detail"]["continued"] is True
        assert [s.get("node") for s in result["spans"]][-1] == "respond", \
            "downstream nodes should still have run"

    def test_the_run_verdict_is_left_alone(self):
        """A flow that tolerates an HTTP failure on purpose must keep
        working. Only the span's honesty changed."""
        with patch("app.egress.safe_http_request",
                   side_effect=SSRFBlocked("blocked")):
            result, _span = _run(_flow("http://localhost:8000/api/health"))
        assert result.get("status") != "error"

    def test_a_request_exception_also_marks_the_span(self):
        # Not only policy blocks: a connection refused, a timeout, a DNS
        # failure all land in the same output variable.
        with patch("app.egress.safe_http_request",
                   side_effect=RuntimeError("connection refused")):
            _result, span = _run(_flow("https://nothing.invalid/x"))
        assert span["status"] == "error"
        assert span["error"]


class TestARealCallIsStillOk:
    def test_a_200_keeps_a_green_span(self):
        def ok(method, url, headers=None, params=None, json=None, data=None, **kw):
            return {"status_code": 200, "text": '{"status":"alive"}', "headers": {},
                    "content": b'{"status":"alive"}', "final_url": url}

        with patch("app.egress.safe_http_request", side_effect=ok):
            _result, span = _run(_flow())
        assert span["status"] == "ok"
        assert "error" not in span

    def test_a_404_is_a_real_answer_not_a_node_failure(self):
        """The node reached the server and got a reply. Calling that a
        node failure would make every deliberate 404 check unusable."""
        def not_found(method, url, headers=None, params=None, json=None, data=None, **kw):
            return {"status_code": 404, "text": "nope", "headers": {},
                    "content": b"nope", "final_url": url}

        with patch("app.egress.safe_http_request", side_effect=not_found):
            _result, span = _run(_flow())
        assert span["status"] == "ok"


class TestTheIntuitiveVariableNamesAreReal:
    """`http_output` was the only variable this node set, and nothing
    advertises that name anywhere. A flow author reaches for
    `{{http_body}}`, gets an empty string, and has no way to find out
    why — which is exactly what happened to Showcase 5."""

    def _vars(self, responder, url="https://example.com/thing"):
        from app.harness import handle_http

        ctx = {"vars": {}}
        with patch("app.egress.safe_http_request", side_effect=responder):
            handle_http({"url": url, "method": "GET", "body_type": "none",
                         "timeout_s": 5}, ctx)
        return ctx["vars"]

    def test_a_success_fills_status_and_body(self):
        def ok(method, url, headers=None, params=None, json=None, data=None, **kw):
            return {"status_code": 200, "text": "the body", "headers": {},
                    "content": b"the body", "final_url": url}

        v = self._vars(ok)
        assert v["http_status"] == 200
        assert v["http_body"] == "the body"
        assert v["http_error"] is None

    def test_the_full_dict_is_still_there_for_existing_flows(self):
        def ok(method, url, headers=None, params=None, json=None, data=None, **kw):
            return {"status_code": 200, "text": "x", "headers": {},
                    "content": b"x", "final_url": url}

        v = self._vars(ok)
        assert v["http_output"]["status_code"] == 200
        assert v["http_output"]["attempts"] == 1

    def test_a_failure_reports_the_reason_and_no_status(self):
        v = self._vars(SSRFBlocked("'localhost' is a blocked hostname"))
        assert v["http_error"] and "blocked" in v["http_error"]
        assert v["http_body"] == ""
        # None, not 0. A condition testing {{http_status}} >= 400 must
        # not quietly pass because a blocked call reported zero.
        assert v["http_status"] is None

    def test_every_exit_path_sets_all_four(self):
        """Four returns in handle_http used to each set the variable
        themselves, which is how three of them would come to forget."""
        def ok(method, url, headers=None, params=None, json=None, data=None, **kw):
            return {"status_code": 204, "text": "", "headers": {},
                    "content": b"", "final_url": url}

        expected = {"http_output", "http_status", "http_body", "http_error"}
        for case in (ok, SSRFBlocked("blocked"), RuntimeError("refused")):
            assert expected <= set(self._vars(case)), f"{case} skipped a variable"
        # The no-URL path returns before any request is attempted.
        from app.harness import handle_http

        ctx = {"vars": {}}
        handle_http({"url": "", "method": "GET"}, ctx)
        assert expected <= set(ctx["vars"])
        assert ctx["vars"]["http_error"] == "no URL configured"
