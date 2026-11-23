"""The FQN-keyed endpoints UCS's object page is meant to point at.

UCS renders Api Access, Logs, Monitoring and Traces for an agent as a
placeholder that says, in its own words: "<tab> is served by the agent
platform ... wiring it means pointing this panel at that service's
endpoint for <fqn>."

UCS knows an agent by its dotted catalogue name, never by this
platform's internal id, so the whole point of these routes is that they
are addressable by FQN. The tests that matter are therefore about
resolution: the right FQN reaches the right agent, and a wrong one does
not quietly reach it anyway.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.db import engine
from app.models import Agent
from app import ucs_client
from sqlmodel import Session, select

client = TestClient(app)


@pytest.fixture(scope="module")
def agent():
    with Session(engine) as s:
        a = s.exec(select(Agent).where(Agent.status == "published")).first()
        assert a is not None, "needs at least one published agent"
        return {"id": a.id, "name": a.name,
                "fqn": ucs_client.agent_fqn(a.id, a.name)}


def test_the_index_declares_what_this_service_serves(agent):
    """So someone wiring a UCS panel can ask, rather than read source."""
    r = client.get(f"/api/catalog/{agent['fqn']}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["agent_id"] == agent["id"]
    assert set(body["surfaces"]) == {
        "api-access", "logs", "traces", "monitoring", "live-jobs"}


@pytest.mark.parametrize("surface", ["api-access", "logs", "traces",
                                     "monitoring", "live-jobs"])
def test_every_declared_surface_answers(agent, surface):
    r = client.get(f"/api/catalog/{agent['fqn']}/{surface}")
    assert r.status_code == 200, r.text


def test_an_fqn_that_names_a_different_object_is_refused(agent):
    """The id is the last segment, so a made-up prefix would otherwise
    resolve to a real agent through a name that is not its own — and the
    catalogue would appear to agree with a wrong FQN."""
    wrong = f"anything.at.all.{agent['id']}"
    r = client.get(f"/api/catalog/{wrong}/logs")
    assert r.status_code == 404
    # The error says what the object IS catalogued as, because the usual
    # cause is a rename.
    assert agent["fqn"] in r.json()["detail"]


def test_a_name_that_is_not_dotted_is_a_bad_request_not_a_lookup(agent):
    r = client.get(f"/api/catalog/{agent['id']}/logs")
    assert r.status_code == 400
    assert "FQN" in r.json()["detail"]


def test_an_unknown_agent_is_not_found(agent):
    r = client.get("/api/catalog/some-slug.agent.nosuchagent/logs")
    assert r.status_code == 404


def test_api_access_reports_the_real_auth_requirement(agent):
    """Whether a caller needs a verified identity is the single most
    consequential thing on that tab."""
    r = client.get(f"/api/catalog/{agent['fqn']}/api-access")
    body = r.json()
    with Session(engine) as s:
        expected = bool((s.get(Agent, agent["id"]).features or {})
                        .get("require_authenticated_user"))
    assert body["requires_authenticated_user"] is expected
    assert body["run_endpoint"] == f"/api/agents/{agent['id']}/run"


def test_monitoring_matches_the_monitoring_page(agent):
    """Two places computing a success rate from the same rows is two
    numbers that can disagree about the same agent."""
    a = client.get(f"/api/catalog/{agent['fqn']}/monitoring").json()["metrics"]
    b = client.get(f"/api/monitoring/metrics?agent_id={agent['id']}").json()["metrics"]
    assert a["success"] == b["success"]
    assert a["p95"] == b["p95"]


def test_live_jobs_only_lists_unfinished_runs(agent):
    r = client.get(f"/api/catalog/{agent['fqn']}/live-jobs")
    for job in r.json()["jobs"]:
        assert job["status"] in ("running", "queued", "paused")


def test_every_surface_identifies_the_object_it_answered_for(agent):
    """A panel showing one agent's logs under another's name is worse
    than an empty tab."""
    for surface in ("api-access", "monitoring", "live-jobs"):
        body = client.get(f"/api/catalog/{agent['fqn']}/{surface}").json()
        assert body["fqn"] == agent["fqn"]
        assert body["agent_id"] == agent["id"]
