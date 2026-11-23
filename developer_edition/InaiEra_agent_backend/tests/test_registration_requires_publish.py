"""An agent enters the UCS catalog when it is published, not when it is
created.

Registration used to happen in create_agent, so a catalog appeared in
Explore the moment someone clicked New agent — for work in progress that
may never be published, may be renamed several times (each rename is a
different catalog, since the catalog is named after the agent), or may be
deleted the same minute.

Publish is the point at which an agent becomes a real, invocable,
governable thing, so that is where it is registered. Which means publish
has to register the MODEL as well as the version: a version cannot be
linked to a model that was never created, and that is the 404 this
produced while creation still did the registering and publish assumed
it had.

The other ways in are closed too — Retry sync refuses for a draft, a
rename does not register one, and the catalog migration skips them.
Otherwise "only published agents are registered" would hold only for
the one path that was changed.
"""
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, text

from app.main import app
from app.db import engine

client = TestClient(app)


def _destroy(agent_id):
    with Session(engine) as s:
        for t in ("run", "trace", "agentlog", "agentversion", "hitlrequest", "role"):
            s.exec(text(f"DELETE FROM {t} WHERE agent_id = :a"), params={"a": agent_id})
        s.exec(text("DELETE FROM agent WHERE id = :a"), params={"a": agent_id})
        s.commit()


@pytest.fixture
def agent():
    r = client.post("/api/agents", json={
        "name": f"Registration gate {uuid.uuid4().hex[:6]}", "subtype": "Custom",
        "app_type": "chatbot", "plane": "App", "owner": "tests"})
    assert r.status_code == 200, r.text
    a = r.json()
    yield a
    _destroy(a["id"])


class TestCreationDoesNotRegister:
    def test_a_new_agent_is_not_registered(self, agent):
        assert agent["ucs_fqn"] is None
        assert agent["ucs_synced"] is False

    def test_nothing_is_even_attempted(self):
        """ucs_synced False with no error means "not registered yet",
        which is a different state from "registration failed" — the UCS
        Access panel reads them differently."""
        with patch("app.ucs_sync.sync_agent") as registered:
            r = client.post("/api/agents", json={
                "name": f"Registration gate {uuid.uuid4().hex[:6]}", "subtype": "Custom",
                "app_type": "chatbot", "plane": "App", "owner": "tests"})
            assert r.status_code == 200, r.text
            try:
                assert not registered.called, "creation reached UCS"
                assert r.json()["ucs_sync_error"] is None
            finally:
                _destroy(r.json()["id"])


class TestPublishRegisters:
    def _publishable(self, agent_id, flow_gov_id):
        r = client.post(f"/api/agents/{agent_id}/nodes",
                        json={"type": "llm", "x": 400, "y": 140})
        assert r.status_code == 200, r.text
        llm = [n["id"] for n in r.json()["flow"]["nodes"] if n["type"] == "llm"][-1]
        client.post(f"/api/agents/{agent_id}/edges",
                    json={"source": flow_gov_id, "target": llm})

    def test_publishing_registers_the_model_and_the_version(self, agent):
        gov = next(n["id"] for n in agent["flow"]["nodes"] if n["type"] == "gov")
        self._publishable(agent["id"], gov)

        with patch("app.ucs_sync.sync_agent") as model, \
             patch("app.ucs_sync.sync_agent_version") as version:
            r = client.post(f"/api/agents/{agent['id']}/publish",
                            params={"invoking_user": "user:tests"})
        assert r.status_code == 200, r.text
        assert model.called, "publish must register the model — the version has nothing to link to"
        assert version.called, "publish must register the version"

    def test_the_model_is_registered_before_the_version(self, agent):
        """Order matters: linking a version to a model that does not
        exist is the 404 that started this."""
        gov = next(n["id"] for n in agent["flow"]["nodes"] if n["type"] == "gov")
        self._publishable(agent["id"], gov)

        order = []
        with patch("app.ucs_sync.sync_agent", side_effect=lambda *a, **k: order.append("model")), \
             patch("app.ucs_sync.sync_agent_version",
                   side_effect=lambda *a, **k: order.append("version")):
            client.post(f"/api/agents/{agent['id']}/publish",
                        params={"invoking_user": "user:tests"})
        assert order[:2] == ["model", "version"], order


class TestTheOtherWaysInAreClosed:
    def test_retry_sync_refuses_for_a_draft(self, agent):
        r = client.post(f"/api/agents/{agent['id']}/ucs/sync")
        assert r.status_code == 409
        detail = r.json()["detail"]
        assert "draft" in detail
        assert "published" in detail

    def test_renaming_a_draft_does_not_register_it(self, agent):
        with patch("app.ucs_sync.sync_agent") as registered:
            r = client.patch(f"/api/agents/{agent['id']}/meta",
                             json={"name": "Renamed while still a draft"})
        assert r.status_code == 200, r.text
        assert not registered.called, "a rename registered a draft in the catalog"
        assert r.json()["ucs_fqn"] is None

    def test_the_catalog_migration_skips_drafts(self, agent):
        with patch("app.ucs_sync.migrate_agent_to_own_catalog") as migrated:
            r = client.post("/api/agents/ucs/migrate-catalogs")
        assert r.status_code == 200, r.text
        skipped = [x for x in r.json()["results"]
                   if x.get("agent_id") == agent["id"] and x.get("skipped") == "draft"]
        assert skipped, "the draft should be reported as skipped, not migrated"
        for call in migrated.call_args_list:
            assert call.args[0] != agent["id"], "a draft was migrated into the catalog"
