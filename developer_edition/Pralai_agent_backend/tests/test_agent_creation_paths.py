"""Every path that creates an agent must give it the default group binding.

The agent-principal half of an entitlement check reads its groups from
the local `role` table (see ucs_client.check_entitlement). An agent with
no rows there is denied every governed object it touches, whatever the
catalog says — so the binding is not a nicety, it is the difference
between an agent that works and one that cannot.

create_agent applied it. The other three paths that construct an Agent
did not:

    create_agent           groups=acme-docs-readers
    template instantiate   groups=(none)              <- denied: ucs
    duplicate              groups=(none)              <- denied: ucs
    import-dsl             groups=(none)              <- denied: ucs

The template gallery is the path people actually use, which is why
"create an agent from a template and try it" reliably produced
"denied: ucs" on a governed flow while an agent built by hand worked.

A duplicate copies the SOURCE's bindings rather than the default, since
a copy that cannot do what it was copied from is broken. That is not an
escalation: the groups are the same local claims, and UCS still decides
whether they hold the grant.
"""
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select, text

from app.main import app
from app.db import engine
from app.models import Role
from app.ucs_provisioning import agent_default_group, auto_grant_group

client = TestClient(app)

DEFAULT_GROUP = auto_grant_group() or agent_default_group()

pytestmark = pytest.mark.skipif(
    not DEFAULT_GROUP,
    reason="no UCS_AGENT_DEFAULT_GROUP/UCS_AUTO_GRANT_GROUP configured, so there is "
           "no default binding to assert (bind_agent_to_default_group is a no-op)")


def _groups(agent_id):
    with Session(engine) as s:
        return sorted(r.group for r in
                      s.exec(select(Role).where(Role.agent_id == agent_id)).all())


def _destroy(agent_id):
    with Session(engine) as s:
        for t in ("run", "trace", "agentlog", "agentversion", "hitlrequest", "role"):
            s.exec(text(f"DELETE FROM {t} WHERE agent_id = :a"), params={"a": agent_id})
        s.exec(text("DELETE FROM agent WHERE id = :a"), params={"a": agent_id})
        s.commit()


@pytest.fixture
def made():
    """Collects every agent a test creates and removes it afterwards, so
    the suite does not deposit its fixtures in the operator's own agent
    list."""
    ids = []
    yield ids
    for aid in ids:
        _destroy(aid)


def _create(made, **overrides):
    body = {"name": f"Creation path {uuid.uuid4().hex[:6]}", "subtype": "Custom",
            "app_type": "chatflow", "plane": "App", "owner": "tests"}
    body.update(overrides)
    r = client.post("/api/agents", json=body)
    assert r.status_code == 200, r.text
    made.append(r.json()["id"])
    return r.json()


class TestEveryCreationPathBinds:
    def test_create_agent_binds(self, made):
        assert _groups(_create(made)["id"]) == [DEFAULT_GROUP]

    def test_template_instantiate_binds(self, made):
        template_id = next(
            t["id"] for t in client.get("/api/templates").json()
            if t["name"] == "Conversational & Knowledge"
        )
        r = client.post(f"/api/templates/{template_id}/instantiate",
                        json={"publish": False, "invoking_user": "user:tests"})
        assert r.status_code == 200, r.text
        aid = r.json()["agent"]["id"]
        made.append(aid)
        assert _groups(aid) == [DEFAULT_GROUP], \
            "a template-instantiated agent has no groups, so every governed object it reads is denied"

    def test_import_dsl_binds(self, made):
        r = client.post("/api/agents/import-dsl", json={"dsl": {
            "name": f"Imported {uuid.uuid4().hex[:6]}", "subtype": "Custom",
            "app_type": "chatflow",
            "flow": {"nodes": [{"id": "n1", "type": "trigger", "x": 0, "y": 0,
                                "sub": "", "config": {}}], "edges": []}}})
        assert r.status_code == 200, r.text
        aid = r.json()["id"]
        made.append(aid)
        assert _groups(aid) == [DEFAULT_GROUP]

    def test_a_duplicate_inherits_the_sources_own_bindings(self, made):
        src = _create(made)
        r = client.post(f"/api/agents/{src['id']}/roles",
                        json={"group": "data-engineers", "tier": "select"})
        assert r.status_code == 200, r.text

        r = client.post(f"/api/agents/{src['id']}/duplicate", json={})
        assert r.status_code == 200, r.text
        clone_id = r.json()["id"]
        made.append(clone_id)
        assert _groups(clone_id) == _groups(src["id"]), \
            "a duplicate cannot do what it was duplicated from"
        assert "data-engineers" in _groups(clone_id)

    def test_a_duplicate_of_an_unbound_agent_still_gets_the_default(self, made):
        src = _create(made)
        with Session(engine) as s:
            s.exec(text("DELETE FROM role WHERE agent_id = :a"), params={"a": src["id"]})
            s.commit()
        assert _groups(src["id"]) == []

        r = client.post(f"/api/agents/{src['id']}/duplicate", json={})
        assert r.status_code == 200, r.text
        clone_id = r.json()["id"]
        made.append(clone_id)
        assert _groups(clone_id) == [DEFAULT_GROUP]


class TestShippedTemplatesReferenceRealObjects:
    def test_no_template_points_at_a_knowledge_base_that_does_not_exist(self):
        """Five of seven shipped templates referenced
        "app_datasets.docs.support_kb" / "lakehouse.core.customer_events",
        neither of which exists in this deployment's catalog, so every
        agent made from them was denied and unpublishable. Reference data
        was insert-only, so fixing the source never reached an existing
        install."""
        from app.flow_compiler import resolve_kb, retrieval_kb_refs

        r = client.get("/api/templates")
        assert r.status_code == 200, r.text
        templates = r.json()
        assert templates, "no templates at all — reference seeding did not run"

        dead = []
        with Session(engine) as s:
            for t in templates:
                for n in (t.get("flow") or {}).get("nodes", []):
                    for ref in retrieval_kb_refs(n.get("config") or {}):
                        if resolve_kb(s, ref) is None:
                            dead.append((t["name"], ref))
        assert dead == [], f"templates reference knowledge bases that do not exist: {dead}"
