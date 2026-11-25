"""Registering an agent in UCS needs its schema, and an already-registered
version is success.

Two separate faults made the UCS Access panel read "not synced" with a
raw Gravitino error, and neither could be cleared from the UI because
Retry sync runs the same two calls.

1. ensure_agent_catalog created the CATALOG but never the SCHEMA.
   Registering a model needs both, so the first REGISTER failed:

     POST /api/v1/admin/models -> 404: model
     'agent_catalog.agents.<id>': ... reason [Schema
     prod_metalake.agent_catalog.agents does not exist]

   The knowledge-base path had always done this correctly
   (ensure_kb_catalog_object creates app_datasets.docs before
   registering a fileset); the agent path had no equivalent step.

2. sync_agent_version sent `[version, "current"]` as the new version's
   aliases. Gravitino allows an alias on one version at a time, so once
   anything held `current`, every later publish was refused:

     409: version of model '...' already exists in Gravitino

   That message names the version, but the conflict is the alias — the
   same call succeeds with a unique alias and fails with `current`.
   Reading the message at face value, the first attempt at this fix
   swallowed the 409 as "already in the desired state", which was worse
   than the bug: publishes reported success while the catalog kept a
   single version, so a second publish never appeared under Explore
   versions. The register call now carries only the version's own
   alias, and `current` is moved separately.

The transport is stubbed here. The wording in these stubs is Gravitino's
own, copied from the real failures.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app import ucs_sync

# Resolved through the module, never bound at import time: another test
# file reloads app.ucs_sync to pick up env changes, which rebinds
# UCSSyncError to a NEW class object. A stale reference here then raised
# an exception the reloaded `except UCSSyncError` no longer matched, and
# these tests failed only when run after that file — a test-ordering
# artifact, not a product fault.
def UCSSyncError(*args):
    return ucs_sync.UCSSyncError(*args)


# ensure_agent_catalog is no longer memoised — see its docstring for why
# remembering "already created" made a deleted catalog unrecoverable.


@pytest.fixture
def recorder(monkeypatch):
    calls = {"admin": [], "gravitino": []}

    def fake_admin_post(path, body, timeout=20.0):
        calls["admin"].append((path, body))
        return {"ok": True}

    def fake_create_schema(catalog, name, comment):
        calls["gravitino"].append(("POST", f"/catalogs/{catalog}/schemas", {"name": name}))
        return {"status": "created"}

    monkeypatch.setattr(ucs_sync, "_admin_post", fake_admin_post)
    monkeypatch.setattr("app.ucs_provisioning._create_schema", fake_create_schema)
    return calls


class TestTheSchemaIsCreated:
    def test_registering_the_catalog_also_creates_the_schema(self, recorder):
        ucs_sync.ensure_agent_catalog("acme-bot-faq")
        catalogs = [p for p, _ in recorder["admin"] if p.endswith("/catalogs")]
        assert catalogs, "the catalog itself should still be registered"
        schema_calls = [c for c in recorder["gravitino"] if c[1].endswith("/schemas")]
        assert schema_calls, "the schema was never created — model registration needs it"
        method, path, body = schema_calls[0]
        assert method == "POST"
        assert path == "/catalogs/acme-bot-faq/schemas"
        assert body["name"] == ucs_sync.UCS_AGENT_SCHEMA

    def test_sync_agent_creates_the_schema_before_the_model(self, recorder):
        ucs_sync.sync_agent("a1", "Revenue Analysis Agent")
        schema_at = next(i for i, c in enumerate(recorder["gravitino"])
                         if c[1].endswith("/schemas"))
        assert schema_at >= 0
        model_calls = [p for p, _ in recorder["admin"] if p.endswith("/models")]
        assert model_calls, "the model should still be registered"

    def test_a_schema_that_already_exists_is_not_an_error(self, monkeypatch, recorder):
        def already(catalog, name, comment):
            raise UCSSyncError("POST /api/v1/admin/schemas -> 409: already exists")
        monkeypatch.setattr("app.ucs_provisioning._create_schema", already)
        ucs_sync.ensure_agent_catalog("acme-bot-faq")  # must not raise

    def test_an_unreachable_gravitino_does_not_block_registration(self, monkeypatch, recorder):
        """The model call reports the real consequence in Gravitino's own
        words, which says more than anything this could invent — so a
        schema failure is logged, not raised."""
        def boom(catalog, name, comment):
            raise RuntimeError("connection refused")
        monkeypatch.setattr("app.ucs_provisioning._create_schema", boom)
        ucs_sync.ensure_agent_catalog("acme-bot-faq")  # must not raise
        assert [p for p, _ in recorder["admin"] if p.endswith("/catalogs")]


class TestVersionRegistration:
    ALREADY = ("POST /api/v1/admin/models/versions -> 409: "
               "{\"detail\":\"version of model 'agent_catalog.agents.a8' "
               "already exists in Gravitino\"}")

    @pytest.fixture(autouse=True)
    def stub_alias_move(self, monkeypatch):
        moves = []
        monkeypatch.setattr(ucs_sync, "_point_current_alias",
                            lambda catalog, agent_id, version:
                                moves.append((catalog, agent_id, version)) or "moved")
        return moves

    def test_the_register_call_never_sends_the_current_alias(self, monkeypatch):
        """The whole cause of the bug. `current` can only be on one
        version, so sending it here makes every publish after the first
        collide."""
        sent = {}

        def ok(path, body, timeout=20.0):
            sent.update(body)
            return {"ok": True}
        monkeypatch.setattr(ucs_sync, "_admin_post", ok)

        ucs_sync.sync_agent_version("a8", "Vendor Onboarding Automation", "v3",
                                             "http://x/api/agents/a8/run")
        assert sent["aliases"] == ["v3"], \
            "only the version's own alias may go on the register call"
        assert "current" not in sent["aliases"]

    def test_current_is_moved_after_registering(self, monkeypatch, stub_alias_move):
        monkeypatch.setattr(ucs_sync, "_admin_post", lambda path, body, timeout=20.0: {"ok": True})
        result = ucs_sync.sync_agent_version("a8", "Vendor Onboarding Automation", "v3",
                                             "http://x/api/agents/a8/run")
        assert stub_alias_move == [("vendor-onboarding-automation", "a8", "v3")]
        assert result["current_alias"] == "moved"

    def test_a_genuine_duplicate_version_is_success(self, monkeypatch):
        """Re-registering a version number that is already there IS the
        desired end state — unlike the alias collision above, which was
        a real failure wearing the same message."""
        def already(path, body, timeout=20.0):
            raise UCSSyncError(self.ALREADY)
        monkeypatch.setattr(ucs_sync, "_admin_post", already)

        result = ucs_sync.sync_agent_version("a8", "Vendor Onboarding Automation", "v3",
                                             "http://x/api/agents/a8/run")
        assert result["already_registered"] is True
        assert result["version"] == "v3"

    def test_a_real_failure_still_raises(self, monkeypatch):
        """Narrow on purpose: a missing model must not be reported as a
        successful sync."""
        def missing(path, body, timeout=20.0):
            raise UCSSyncError("POST /api/v1/admin/models/versions -> 404: "
                               "{\"detail\":\"Model ... does not exist\"}")
        monkeypatch.setattr(ucs_sync, "_admin_post", missing)

        with pytest.raises(ucs_sync.UCSSyncError):
            ucs_sync.sync_agent_version("a8", "Vendor Onboarding Automation", "v3",
                                             "http://x/api/agents/a8/run")

    def test_a_first_registration_still_goes_through(self, monkeypatch):
        sent = {}

        def ok(path, body, timeout=20.0):
            sent.update({"path": path, "body": body})
            return {"ok": True}
        monkeypatch.setattr(ucs_sync, "_admin_post", ok)

        ucs_sync.sync_agent_version("a8", "Vendor Onboarding Automation", "v3",
                                    "http://x/api/agents/a8/run", "Published v3")
        assert sent["path"] == "/api/v1/admin/models/versions"
        assert sent["body"]["model"] == "a8"
        assert sent["body"]["aliases"] == ["v3"]


class TestEachAgentGetsItsOwnCatalog:
    """Agents used to be models inside one shared `agent_catalog.agents`,
    keyed by id, which is what the Explore tree showed — rows of opaque
    ids under a single catalog. Each agent is now its own catalog, named
    after the agent.

    Slugified rather than the literal name, because Gravitino refuses
    both of the things agent names actually contain. Measured against
    the running catalog:

        'Acme bot FAQ'  -> 400, operation [CREATE] refused
        'Acme.bot.FAQ'  -> 400, operation [CREATE] refused
        'acme-bot-faq'  -> created
    """

    def test_the_catalog_is_named_after_the_agent(self):
        from app.ucs_client import agent_slug, agent_fqn
        assert agent_slug("Acme bot FAQ") == "acme-bot-faq"
        assert agent_fqn("a04c5406699", "Acme bot FAQ") == "acme-bot-faq.agent.a04c5406699"

    def test_the_object_is_still_the_id(self):
        """A rename changes the name, never the id — so the id stays the
        thing a grant is anchored to."""
        from app.ucs_client import agent_fqn
        assert agent_fqn("a4", "Churn Model Scientist").endswith(".agent.a4")

    def test_characters_gravitino_refuses_are_removed(self):
        from app.ucs_client import agent_slug
        assert " " not in agent_slug("Acme bot FAQ")
        # A dot would be worse than invalid: rbac_cache.decide() splits an
        # object on '.', so one inside the catalog name would silently
        # re-cut the FQN and scope the grant to something else.
        assert "." not in agent_slug("Acme.bot.FAQ")

    def test_a_name_that_slugifies_to_nothing_falls_back(self):
        from app.ucs_client import agent_slug, UCS_AGENT_CATALOG
        assert agent_slug("###") == UCS_AGENT_CATALOG
        assert agent_slug("") == UCS_AGENT_CATALOG

    def test_sync_agent_registers_under_the_agents_own_catalog(self, recorder):
        ucs_sync.sync_agent("a04c5406699", "Acme bot FAQ")
        models = [b for p, b in recorder["admin"] if p.endswith("/models")]
        assert models, "the model should be registered"
        assert models[0]["catalog"] == "acme-bot-faq"
        assert models[0]["name"] == "a04c5406699"

    def test_a_grant_is_scoped_to_that_catalog(self, recorder):
        ucs_sync.grant_agent_access("a04c5406699", "Acme bot FAQ", "finance")
        roles = [b for p, b in recorder["admin"] if p.endswith("/rbac/roles")]
        assert roles, "the role should be created"
        assert roles[0]["catalog"] == "acme-bot-faq"
        assert roles[0]["object_name"] == "a04c5406699"

    def test_an_existing_role_has_the_new_scope_added(self, monkeypatch):
        """The role NAME carries no catalog, so a role created while the
        agent lived in the shared catalog keeps that old scope and
        re-creating it is a no-op. A grant then reported success and
        allowed nothing — measured, before this branch existed:

            role agent-a04c5406699-select
            scope agent_catalog.agents.a04c5406699   (unchanged)
            authz check on acme-bot-faq.agent.a04c5406699 -> deny
        """
        calls = []

        def admin(path, body, timeout=20.0):
            calls.append((path, body))
            if path.endswith("/rbac/roles"):
                raise UCSSyncError("POST /api/v1/admin/rbac/roles -> 409: already exists")
            return {"ok": True}
        monkeypatch.setattr(ucs_sync, "_admin_post", admin)

        ucs_sync.grant_agent_access("a04c5406699", "Acme bot FAQ", "finance")
        privilege_calls = [(p, b) for p, b in calls if "/privileges" in p]
        assert privilege_calls, "the new scope was never added to the existing role"
        path, body = privilege_calls[0]
        assert path == "/api/v1/admin/rbac/roles/agent-a04c5406699-select/privileges"
        assert body["catalog"] == "acme-bot-faq"
        assert body["object_name"] == "a04c5406699"


class TestADeletedCatalogCanBeRecreated:
    """Removing an agent's catalog in UCS must not be terminal.

    ensure_agent_catalog used to remember which catalogs it had created
    and return early for those. Delete one in UCS and the process kept
    believing it existed, so every later publish skipped creating it and
    died at the next step:

      POST /api/v1/admin/models -> 404: ... reason
      [Catalog prod_metalake.test-generator does not exist]

    Neither publishing again nor Retry sync could recover, because the
    one call that would rebuild the catalog was the one being skipped.
    """

    def test_the_catalog_is_ensured_on_every_call(self, recorder):
        ucs_sync.sync_agent("a1", "Acme bot FAQ")
        ucs_sync.sync_agent("a1", "Acme bot FAQ")
        creates = [b for p, b in recorder["admin"] if p.endswith("/catalogs")]
        assert len(creates) == 2, \
            "the second call skipped ensuring the catalog, so a deleted one never comes back"

    def test_a_missing_catalog_is_rebuilt_and_the_register_retried(self, monkeypatch):
        """The reported failure, end to end: the catalog is gone, the
        REGISTER says so, and the next attempt must succeed rather than
        return the same 404."""
        calls = []
        attempts = {"register": 0}

        def admin(path, body, timeout=20.0):
            calls.append(path)
            if path.endswith("/models"):
                attempts["register"] += 1
                if attempts["register"] == 1:
                    raise ucs_sync.UCSSyncError(
                        "POST /api/v1/admin/models -> 404: {\"detail\":\"model "
                        "'test-generator.agent.ac466a9ecb5': Failed to operate model(s) "
                        "[ac466a9ecb5] operation [REGISTER] under schema [agent], reason "
                        "[Catalog prod_metalake.test-generator does not exist]\"}")
            return {"ok": True}

        monkeypatch.setattr(ucs_sync, "_admin_post", admin)

        result = ucs_sync.sync_agent("ac466a9ecb5", "test generator")
        assert attempts["register"] == 2, "the register was not retried after rebuilding"
        assert result == {"ok": True}
        # and the catalog was ensured again between the two attempts
        assert calls.count("/api/v1/admin/catalogs") == 2

    def test_a_genuine_rejection_is_not_retried_forever(self, monkeypatch):
        """Only a missing container triggers the rebuild. Anything else
        must surface, not loop."""
        attempts = {"n": 0}

        def admin(path, body, timeout=20.0):
            if path.endswith("/models"):
                attempts["n"] += 1
                raise ucs_sync.UCSSyncError(
                    "POST /api/v1/admin/models -> 500: {\"detail\":\"internal error\"}")
            return {"ok": True}

        monkeypatch.setattr(ucs_sync, "_admin_post", admin)

        with pytest.raises(ucs_sync.UCSSyncError):
            ucs_sync.sync_agent("a1", "Acme bot FAQ")
        assert attempts["n"] == 1
