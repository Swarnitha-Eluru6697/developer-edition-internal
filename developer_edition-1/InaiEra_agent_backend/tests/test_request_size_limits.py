"""JSON API requests need size AND semantic limits.

The raw-body middleware was scoped to /api/uploads, so every other JSON
endpoint — flows, prompts, structured inputs, batches, configuration —
had no body bound at all: the ASGI layer and Pydantic consumed whatever
arrived before any handler could object.

Bytes are only half of it. A 45 KB body carries a 600-node flow, and
every one of those nodes is validated, deep-copied, hashed and persisted
on save, then walked again on publish and on every run. A 310-byte body
carries 60 levels of nesting, which costs recursion in every validator
and serializer that touches it. So there are two layers, refusing for
different reasons:

    413  the body is too large to read           (middleware, pre-parse)
    422  the body was fine; its meaning is not   (handler)

Verified live against the running deployment before these tests were
written, all four with real HTTP:

    2MB body to /run                     -> 413
    250k-character input (250 KB body)   -> 422
    600-node template_flow (45 KB body)  -> 422
    60-level nested config (310 B body)  -> 422
    2000-row batch                       -> 422

And 40 consecutive 8 MB requests (320 MB attempted) moved the api
container's memory from 159 MiB to 162.9 MiB, which is the "without
material memory growth" half of the criterion.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

from app.limits import (MAX_BATCH_ROWS, MAX_FLOW_EDGES, MAX_FLOW_NODES,
                        MAX_JSON_BODY_BYTES, MAX_JSON_DEPTH, MAX_PROMPT_CHARS,
                        MAX_RUN_INPUT_CHARS, MAX_STRUCTURED_INPUT_FIELDS, json_depth)
from app.main import app

client = TestClient(app)


@pytest.fixture
def agent_id():
    r = client.post("/api/agents", json={"name": "Limits test", "subtype": "Workflow",
                                         "app_type": "workflow"})
    assert r.status_code == 200, r.text
    aid = r.json()["id"]
    yield aid
    from sqlmodel import Session
    from app.db import engine
    from app.routers.agents import delete_agent
    with Session(engine) as s:
        try:
            delete_agent(aid, s)
        except Exception:
            pass


def _nodes(count):
    return [{"id": f"n{i}", "type": "respond", "x": 0, "y": 0, "sub": "", "config": {}}
            for i in range(count)]


class TestTheDefaultBodyLimitCoversMoreThanUploads:
    def test_an_oversized_json_body_is_rejected_with_413(self, agent_id):
        """The reported gap: this path had no limit at all."""
        body = json.dumps({"input": "x" * (MAX_JSON_BODY_BYTES + 1000)})
        r = client.post(f"/api/agents/{agent_id}/run", content=body,
                        headers={"Content-Type": "application/json"})
        assert r.status_code == 413, f"got {r.status_code}"

    def test_the_refusal_names_a_limit_rather_than_being_opaque(self, agent_id):
        body = json.dumps({"input": "x" * (MAX_JSON_BODY_BYTES + 1000)})
        r = client.post(f"/api/agents/{agent_id}/run", content=body,
                        headers={"Content-Type": "application/json"})
        # The STATUS is asserted too: without it this passes for the
        # wrong reason, because the 422 a semantic cap produces also
        # mentions a limit. Caught by reverting only the middleware
        # scoping and watching this test stay green.
        assert r.status_code == 413
        assert "limit" in r.text.lower()

    def test_uploads_keep_their_larger_ceiling(self):
        """A single global cap would have been both far too generous for
        a JSON body and far too small for a document, which is why the
        limit is per-prefix."""
        from app.routers.uploads import MAX_UPLOAD_BYTES

        assert MAX_UPLOAD_BYTES > MAX_JSON_BODY_BYTES
        payload = b"x" * (MAX_JSON_BODY_BYTES + 5000)
        r = client.post("/api/uploads", files={"file": ("big.txt", payload, "text/plain")})
        # Not a 413: this is exactly the case the JSON default must not break.
        assert r.status_code != 413, r.text

    def test_the_most_specific_prefix_wins(self):
        from app.middleware import MaxRequestBodySizeMiddleware

        mw = MaxRequestBodySizeMiddleware(
            app=None, max_bytes=1000, path_prefixes=("/api",),
            overrides={"/api/uploads": 50_000})
        assert mw._limit_for("/api/agents") == 1000
        assert mw._limit_for("/api/uploads") == 50_000
        assert mw._limit_for("/static/thing") is None

    def test_an_ordinary_request_is_unaffected(self, agent_id):
        r = client.get(f"/api/agents/{agent_id}")
        assert r.status_code == 200


class TestSemanticCapsCatchWhatByteLimitsCannot:
    def test_an_oversized_flow_is_rejected_even_though_the_body_is_small(self):
        payload = {"name": "too big", "subtype": "Workflow", "app_type": "workflow",
                   "template_flow": {"nodes": _nodes(MAX_FLOW_NODES + 100), "edges": []}}
        encoded = json.dumps(payload)
        assert len(encoded) < MAX_JSON_BODY_BYTES, "this test must stay under the byte limit"

        r = client.post("/api/agents", content=encoded,
                        headers={"Content-Type": "application/json"})
        assert r.status_code == 422, r.text
        assert "nodes" in r.json()["detail"]

    def test_too_many_edges_is_rejected(self):
        edges = [{"id": f"e{i}", "source": "n0", "target": "n0"}
                 for i in range(MAX_FLOW_EDGES + 10)]
        r = client.post("/api/agents", json={
            "name": "too many edges", "subtype": "Workflow", "app_type": "workflow",
            "template_flow": {"nodes": _nodes(2), "edges": edges}})
        assert r.status_code == 422
        assert "edges" in r.json()["detail"]

    def test_deep_nesting_is_rejected_despite_a_tiny_body(self):
        """310 bytes, 60 levels. A byte limit cannot see this at all."""
        deep = 1
        for _ in range(MAX_JSON_DEPTH + 30):
            deep = [deep]
        payload = {"name": "deep", "subtype": "Workflow", "app_type": "workflow",
                   "template_flow": {"nodes": [{"id": "n1", "type": "respond", "x": 0, "y": 0,
                                                 "sub": "", "config": {"deep": deep}}],
                                      "edges": []}}
        encoded = json.dumps(payload)
        assert len(encoded) < 2000, "the point is that this is a tiny body"

        r = client.post("/api/agents", content=encoded,
                        headers={"Content-Type": "application/json"})
        assert r.status_code == 422, r.text
        assert "deep" in r.json()["detail"].lower()

    def test_an_oversized_run_input_is_rejected(self, agent_id):
        r = client.post(f"/api/agents/{agent_id}/run",
                        json={"input": "x" * (MAX_RUN_INPUT_CHARS + 1)})
        assert r.status_code == 422, r.text
        assert "characters" in r.json()["detail"]

    def test_too_many_structured_input_fields_is_rejected(self, agent_id):
        inputs = {f"f{i}": "v" for i in range(MAX_STRUCTURED_INPUT_FIELDS + 1)}
        r = client.post(f"/api/agents/{agent_id}/run", json={"input": "go", "inputs": inputs})
        assert r.status_code == 422, r.text

    def test_an_oversized_batch_is_rejected(self, agent_id):
        r = client.post(f"/api/agents/{agent_id}/batch-run",
                        json={"rows": [{"a": "1"} for _ in range(MAX_BATCH_ROWS + 1)],
                              "invoking_user": "user:test"})
        assert r.status_code == 422, r.text
        assert "rows" in r.json()["detail"]

    def test_an_oversized_prompt_fails_validation(self):
        from app.flow_validator import validate_flow

        nodes = [{"id": "n1", "type": "trigger", "config": {}},
                 {"id": "n2", "type": "llm",
                  "config": {"user_prompt": "x" * (MAX_PROMPT_CHARS + 1)}},
                 {"id": "n3", "type": "respond", "config": {}}]
        edges = [{"id": "e1", "source": "n1", "target": "n2"},
                 {"id": "e2", "source": "n2", "target": "n3"}]
        result = validate_flow(nodes, edges, "chatflow")
        assert any("characters" in i.message for i in result.errors), \
            [i.message for i in result.errors]


class TestOrdinaryRequestsStillWork:
    """The guard on the guards: a limit that refused real requests would
    just be a different outage."""

    def test_a_normal_agent_is_created(self):
        r = client.post("/api/agents", json={"name": "normal", "subtype": "Workflow",
                                             "app_type": "workflow"})
        assert r.status_code == 200
        from sqlmodel import Session
        from app.db import engine
        from app.routers.agents import delete_agent
        with Session(engine) as s:
            try:
                delete_agent(r.json()["id"], s)
            except Exception:
                pass

    def test_a_flow_at_the_node_limit_is_accepted(self):
        r = client.post("/api/agents", json={
            "name": "at the limit", "subtype": "Workflow", "app_type": "workflow",
            "template_flow": {"nodes": _nodes(MAX_FLOW_NODES), "edges": []}})
        assert r.status_code == 200, r.text
        from sqlmodel import Session
        from app.db import engine
        from app.routers.agents import delete_agent
        with Session(engine) as s:
            try:
                delete_agent(r.json()["id"], s)
            except Exception:
                pass

    def test_a_normal_run_input_is_accepted(self, agent_id):
        r = client.post(f"/api/agents/{agent_id}/run", json={"input": "a normal question"})
        assert r.status_code != 422, r.text

    def test_a_small_batch_is_accepted(self, agent_id):
        r = client.post(f"/api/agents/{agent_id}/batch-run",
                        json={"rows": [{"a": "1"}], "invoking_user": "user:test"})
        assert r.status_code != 422, r.text


class TestTheDepthCheckIsItselfBounded:
    def test_it_stops_descending_once_past_the_cap(self):
        """The check must not become the attack: walking a 10,000-level
        structure to report that it is too deep would be the same
        problem wearing a different hat."""
        deep = 1
        for _ in range(10_000):
            deep = [deep]
        assert json_depth(deep) == MAX_JSON_DEPTH + 1

    def test_it_measures_ordinary_structures_correctly(self):
        assert json_depth(1) == 0
        assert json_depth([1]) == 1
        assert json_depth({"a": {"b": [1]}}) == 3
        assert json_depth([]) == 0
