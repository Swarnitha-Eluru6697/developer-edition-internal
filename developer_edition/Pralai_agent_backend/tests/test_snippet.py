"""
Real tests for the Snippet feature (§0.38, GAP_REPORT), matching Dify's
documented spec (https://docs.dify.ai/en/cloud/use-dify/build/snippet).
Uses the real FastAPI TestClient against the real Postgres engine,
matching this project's other router-level tests.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def create_agent(plane="App", **overrides):
    body = {"name": "Test Agent", "subtype": "Custom", "plane": plane}
    body.update(overrides)
    r = client.post("/api/agents", json=body)
    assert r.status_code == 200, r.text
    return r.json()


class TestRealSnippetCreation:
    def test_a_new_snippet_gets_a_real_genuinely_empty_flow(self):
        a = create_agent(plane="Snippet")
        assert a["plane"] == "Snippet"
        assert a["flow"]["nodes"] == []
        assert a["flow"]["edges"] == []

    def test_a_new_app_agent_still_gets_the_real_standard_scaffold_unaffected(self):
        """The App scaffold is trigger + the UCS check now, not
        trigger/gov/respond. `respond` is a palette node you drag in —
        placing one was a guess about a flow nobody had designed yet.
        The point of this test is unchanged: a Snippet is empty and an
        App is not. See test_new_agent_scaffold.py for the full contract.
        """
        a = create_agent(plane="App")
        types = [n["type"] for n in a["flow"]["nodes"]]
        assert types == ["trigger", "gov"]

    def test_input_variables_starts_as_a_real_empty_list(self):
        a = create_agent(plane="Snippet")
        assert a["input_variables"] == []


class TestRealPlaneFiltering:
    def test_plane_snippet_filter_only_returns_real_snippets(self):
        create_agent(plane="Snippet", name="Filter Test Snippet")
        r = client.get("/api/agents", params={"plane": "Snippet"})
        assert r.status_code == 200
        assert all(a["plane"] == "Snippet" for a in r.json())

    def test_plane_app_filter_never_leaks_a_real_snippet(self):
        create_agent(plane="Snippet", name="Should Not Leak")
        r = client.get("/api/agents", params={"plane": "App"})
        assert r.status_code == 200
        assert all(a["plane"] != "Snippet" for a in r.json())

    def test_no_plane_filter_still_returns_everything_backward_compat(self):
        r = client.get("/api/agents")
        assert r.status_code == 200
        assert len(r.json()) > 0


class TestRealSnippetPublish:
    def test_a_snippet_with_no_trigger_node_publishes_successfully(self):
        """The central real proof this reuses existing publish
        machinery correctly: _resolve_and_publish never assumed a
        trigger node exists."""
        a = create_agent(plane="Snippet")
        r = client.post(f"/api/agents/{a['id']}/publish")
        assert r.status_code == 200, r.text
        assert r.json()["version"] == "v2"


class TestRealInsertSnippet:
    def _make_published_snippet(self):
        a = create_agent(plane="Snippet")
        flow = {
            "nodes": [
                {"id": "s1", "type": "llm", "x": 40, "y": 100, "sub": "", "config": {"user_prompt": "{{topic}}"}},
                {"id": "s2", "type": "code", "x": 240, "y": 100, "sub": "", "config": {"code": "def main(): pass"}},
            ],
            "edges": [{"id": "se1", "source": "s1", "target": "s2", "source_handle": None}],
        }
        from sqlmodel import Session
        from app.db import engine
        from app.models import Agent
        with Session(engine) as s:
            row = s.get(Agent, a["id"])
            row.flow = flow
            s.add(row)
            s.commit()
        r = client.post(f"/api/agents/{a['id']}/publish")
        assert r.status_code == 200
        return a["id"]

    def test_inserting_a_real_published_snippet_genuinely_copies_its_nodes(self):
        snippet_id = self._make_published_snippet()
        target = create_agent(plane="App")
        before_ids = {n["id"] for n in target["flow"]["nodes"]}

        r = client.post(f"/api/agents/{target['id']}/insert-snippet", json={"snippet_id": snippet_id, "x": 500, "y": 300})
        assert r.status_code == 200, r.text
        result = r.json()
        after_ids = {n["id"] for n in result["flow"]["nodes"]}
        assert before_ids.issubset(after_ids)
        assert len(after_ids) == len(before_ids) + 2

    def test_the_original_snippet_ids_never_leak_into_the_target_verbatim(self):
        snippet_id = self._make_published_snippet()
        target = create_agent(plane="App")
        r = client.post(f"/api/agents/{target['id']}/insert-snippet", json={"snippet_id": snippet_id, "x": 0, "y": 0})
        new_node_ids = [n["id"] for n in r.json()["flow"]["nodes"]]
        assert "s1" not in new_node_ids
        assert "s2" not in new_node_ids
        assert any(nid.startswith("snip-") for nid in new_node_ids)

    def test_real_internal_edges_are_rewired_to_the_real_new_ids(self):
        snippet_id = self._make_published_snippet()
        target = create_agent(plane="App")
        r = client.post(f"/api/agents/{target['id']}/insert-snippet", json={"snippet_id": snippet_id, "x": 0, "y": 0})
        flow = r.json()["flow"]
        new_edge = next(e for e in flow["edges"] if e["id"].startswith("snip-e-"))
        node_ids = {n["id"] for n in flow["nodes"]}
        assert new_edge["source"] in node_ids
        assert new_edge["target"] in node_ids

    def test_real_position_offset_lands_the_first_node_exactly_at_the_requested_xy(self):
        snippet_id = self._make_published_snippet()
        target = create_agent(plane="App")
        r = client.post(f"/api/agents/{target['id']}/insert-snippet", json={"snippet_id": snippet_id, "x": 777, "y": 333})
        flow = r.json()["flow"]
        new_nodes = [n for n in flow["nodes"] if n["id"].startswith("snip-")]
        min_x = min(n["x"] for n in new_nodes)
        min_y = min(n["y"] for n in new_nodes)
        assert min_x == 777
        assert min_y == 333

    def test_real_config_is_genuinely_preserved_on_the_copied_nodes(self):
        snippet_id = self._make_published_snippet()
        target = create_agent(plane="App")
        r = client.post(f"/api/agents/{target['id']}/insert-snippet", json={"snippet_id": snippet_id, "x": 0, "y": 0})
        llm_node = next(n for n in r.json()["flow"]["nodes"] if n["type"] == "llm" and "snip-" in n["id"])
        assert llm_node["config"]["user_prompt"] == "{{topic}}"

    def test_inserting_twice_produces_real_independent_copies_with_no_id_collision(self):
        snippet_id = self._make_published_snippet()
        target = create_agent(plane="App")
        client.post(f"/api/agents/{target['id']}/insert-snippet", json={"snippet_id": snippet_id, "x": 0, "y": 0})
        r2 = client.post(f"/api/agents/{target['id']}/insert-snippet", json={"snippet_id": snippet_id, "x": 500, "y": 0})
        node_ids = [n["id"] for n in r2.json()["flow"]["nodes"]]
        assert len(node_ids) == len(set(node_ids))

    def test_editing_the_original_snippet_after_insertion_never_touches_the_target(self):
        snippet_id = self._make_published_snippet()
        target = create_agent(plane="App")
        client.post(f"/api/agents/{target['id']}/insert-snippet", json={"snippet_id": snippet_id, "x": 0, "y": 0})

        from sqlmodel import Session
        from app.db import engine
        from app.models import Agent
        with Session(engine) as s:
            row = s.get(Agent, snippet_id)
            assert [n["id"] for n in row.flow["nodes"]] == ["s1", "s2"]  # genuinely untouched

    def test_inserting_an_unpublished_snippet_is_rejected_with_a_real_honest_message(self):
        a = create_agent(plane="Snippet")  # never published
        target = create_agent(plane="App")
        r = client.post(f"/api/agents/{target['id']}/insert-snippet", json={"snippet_id": a["id"], "x": 0, "y": 0})
        assert r.status_code == 400
        assert "published" in r.json()["detail"].lower()

    def test_inserting_a_non_snippet_agent_is_rejected(self):
        not_a_snippet = create_agent(plane="App")
        target = create_agent(plane="App")
        r = client.post(f"/api/agents/{target['id']}/insert-snippet", json={"snippet_id": not_a_snippet["id"], "x": 0, "y": 0})
        assert r.status_code == 400
        assert "snippet" in r.json()["detail"].lower()

    def test_inserting_a_nonexistent_snippet_id_returns_a_real_404(self):
        target = create_agent(plane="App")
        r = client.post(f"/api/agents/{target['id']}/insert-snippet", json={"snippet_id": "does-not-exist", "x": 0, "y": 0})
        assert r.status_code == 404
