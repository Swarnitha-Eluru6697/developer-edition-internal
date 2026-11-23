"""
Real tests for the node/edge ID collision fix (section 0.72,
GAP_REPORT).

The real bug, exactly as reported: new node/edge IDs were generated as
f"n{len(nodes)+1}_{timestamp_ms % 100000}" - combining a value that
repeats after every delete (the node count) with a value that wraps
every real 100 seconds (timestamp mod 100000). Two real, distinct
nodes could land on the identical id - not only under genuine
concurrency, but even sequentially if a node was added, deleted, and
another added within the same real 100-second window with the count
back to the same value.

The fix: a real uuid4 hex slice, matching the same convention every
other real id in this router already uses (KnowledgeMetadataField's
"kmf-" prefix, this router's own "snip-" prefix for inserted snippet
nodes/edges) - a genuinely negligible collision probability regardless
of how many nodes/edges a real flow has or how quickly they're added.

These tests exercise the real, running API directly, creating many
real nodes/edges in rapid succession - not mocked.
"""
import sys
import os
import re
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, text

from app.db import engine
from app.main import app

client = TestClient(app)


@pytest.fixture()
def real_agent():
    r = client.post("/api/agents", json={
        "name": f"ID Collision Test {uuid.uuid4().hex[:8]}", "subtype": "Custom", "app_type": "workflow",
        "template_flow": {
            "nodes": [{"id": "n1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}}],
            "edges": [],
        },
    })
    agent_id = r.json()["id"]
    yield agent_id
    with Session(engine) as s:
        s.exec(text("DELETE FROM agentlog WHERE agent_id = :a"), params={"a": agent_id})
        s.exec(text("DELETE FROM agent WHERE id = :a"), params={"a": agent_id})
        s.commit()


class TestRealNodeIdUniqueness:
    def test_forty_rapid_real_node_creations_produce_forty_unique_real_ids(self, real_agent):
        rev = client.get(f"/api/agents/{real_agent}").json()["draft_revision"]
        node_ids = []
        for i in range(40):
            r = client.post(f"/api/agents/{real_agent}/nodes",
                             json={"type": "llm", "x": i, "y": i, "sub": "", "config": {}},
                             headers={"If-Match": str(rev)})
            assert r.status_code == 200
            body = r.json()
            rev = body["draft_revision"]
            node_ids.append(body["flow"]["nodes"][-1]["id"])

        assert len(node_ids) == 40
        assert len(set(node_ids)) == 40

    def test_a_new_node_id_uses_the_real_uuid_based_format_not_the_old_count_plus_timestamp_scheme(self, real_agent):
        r = client.post(f"/api/agents/{real_agent}/nodes",
                         json={"type": "llm", "x": 0, "y": 0, "sub": "", "config": {}},
                         headers={"If-Match": "1"})
        new_id = r.json()["flow"]["nodes"][-1]["id"]
        assert re.fullmatch(r"n[0-9a-f]{10}", new_id), f"unexpected node id format: {new_id!r}"

    def test_deleting_and_recreating_nodes_repeatedly_never_collides(self, real_agent):
        rev = client.get(f"/api/agents/{real_agent}").json()["draft_revision"]
        seen_ids = set()
        for _ in range(15):
            r = client.post(f"/api/agents/{real_agent}/nodes",
                             json={"type": "llm", "x": 0, "y": 0, "sub": "", "config": {}},
                             headers={"If-Match": str(rev)})
            body = r.json()
            rev = body["draft_revision"]
            new_id = body["flow"]["nodes"][-1]["id"]
            assert new_id not in seen_ids
            seen_ids.add(new_id)

            r2 = client.delete(f"/api/agents/{real_agent}/nodes/{new_id}", headers={"If-Match": str(rev)})
            rev = r2.json()["draft_revision"]


class TestRealEdgeIdUniqueness:
    def test_twenty_rapid_real_fanout_edges_produce_twenty_unique_real_ids(self, real_agent):
        rev_body = client.get(f"/api/agents/{real_agent}").json()
        rev = rev_body["draft_revision"]

        target_ids = []
        for i in range(20):
            r = client.post(f"/api/agents/{real_agent}/nodes",
                             json={"type": "llm", "x": i, "y": i, "sub": "", "config": {}},
                             headers={"If-Match": str(rev)})
            body = r.json()
            rev = body["draft_revision"]
            target_ids.append(body["flow"]["nodes"][-1]["id"])

        edge_ids = []
        for target in target_ids:
            r = client.post(f"/api/agents/{real_agent}/edges",
                             json={"source": "n1", "target": target, "source_handle": None},
                             headers={"If-Match": str(rev)})
            assert r.status_code == 200
            body = r.json()
            rev = body["draft_revision"]
            edge_ids.append(body["flow"]["edges"][-1]["id"])

        assert len(edge_ids) == 20
        assert len(set(edge_ids)) == 20

    def test_a_new_edge_id_uses_the_real_uuid_based_format(self, real_agent):
        rev = client.get(f"/api/agents/{real_agent}").json()["draft_revision"]
        r = client.post(f"/api/agents/{real_agent}/nodes",
                         json={"type": "llm", "x": 0, "y": 0, "sub": "", "config": {}},
                         headers={"If-Match": str(rev)})
        body = r.json()
        rev = body["draft_revision"]
        target = body["flow"]["nodes"][-1]["id"]

        r2 = client.post(f"/api/agents/{real_agent}/edges",
                          json={"source": "n1", "target": target, "source_handle": None},
                          headers={"If-Match": str(rev)})
        new_edge_id = r2.json()["flow"]["edges"][-1]["id"]
        assert re.fullmatch(r"e[0-9a-f]{10}", new_edge_id), f"unexpected edge id format: {new_edge_id!r}"
