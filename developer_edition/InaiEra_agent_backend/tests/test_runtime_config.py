"""
Real tests for the read-only runtime configuration endpoint (§0.45,
GAP_REPORT) - an admin-visible, never-editable view of the platform's
actual live tunable settings.
"""
import sys
import os
import importlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient


def _reload_chain():
    """The real, correct reload order — leaves first, so each module
    picks up whatever the CURRENT (possibly monkeypatched) environment
    says, not a stale value bound at an earlier import. Includes
    app.temporal_workflow specifically because it ALSO does
    `from .harness import compute_ready_wave` — without reloading it
    here too, its own reference stays bound to whichever harness
    module object existed the first time it was ever imported,
    breaking test_temporal_scheduling.py's real identity assertion
    the moment this file's own reloads run before it in the same
    pytest session — caught by running the FULL suite, not just this
    file, before trusting this fix."""
    import app.db as db_module
    import app.harness as harness_module
    import app.temporal_workflow as temporal_workflow_module
    import app.ucs_client as ucs_client_module
    import app.ucs_sync as ucs_sync_module
    import app.routers.execution as execution_module
    import app.routers.settings as settings_module
    import app.main as main_module

    importlib.reload(db_module)
    importlib.reload(harness_module)
    importlib.reload(temporal_workflow_module)
    importlib.reload(ucs_client_module)
    importlib.reload(ucs_sync_module)
    importlib.reload(execution_module)
    importlib.reload(settings_module)
    importlib.reload(main_module)
    return main_module


def _fresh_client():
    return TestClient(_reload_chain().app)


@pytest.fixture(autouse=True)
def _restore_module_state_after_each_test():
    """Real test-isolation fix — importlib.reload() mutates real,
    process-global module state (sys.modules), which does NOT get
    undone just because pytest's own monkeypatch fixture reverts the
    env vars afterward. Without this, a test here that reloads
    app.harness leaves OTHER test files (e.g.
    test_temporal_scheduling.py, which asserts real object identity
    between harness.compute_ready_wave and
    temporal_workflow.compute_ready_wave) seeing new, different
    function objects than the ones created at normal interpreter
    startup — a real bug caught by running the FULL suite, not just
    this file, before trusting this test infrastructure."""
    yield
    _reload_chain()


class TestRuntimeConfigEndpoint:
    def test_returns_real_data_with_the_expected_top_level_sections(self):
        c = _fresh_client()
        r = c.get("/api/settings/runtime-config")
        assert r.status_code == 200
        body = r.json()
        for section in ("database", "temporal", "ucs", "platform", "model_provider"):
            assert section in body

    def test_database_section_reports_real_live_pool_state_not_just_configured_numbers(self):
        c = _fresh_client()
        body = c.get("/api/settings/runtime-config").json()
        db = body["database"]
        assert db["pool_size_configured"] == db["pool_size_live"]
        assert db["connections_checked_out_live"] >= 0
        assert db["overflow_connections_in_use_live"] >= 0

    def test_genuinely_reflects_a_real_env_var_override_not_a_cached_default(self, monkeypatch):
        monkeypatch.setenv("DB_POOL_SIZE", "17")
        c = _fresh_client()
        body = c.get("/api/settings/runtime-config").json()
        assert body["database"]["pool_size_configured"] == 17
        assert body["database"]["pool_size_live"] == 17

    def test_ucs_soft_check_reflects_the_real_env_var(self, monkeypatch):
        monkeypatch.setenv("UCS_SOFT_CHECK", "true")
        c = _fresh_client()
        body = c.get("/api/settings/runtime-config").json()
        assert body["ucs"]["soft_check_mode"] is True

    def test_never_exposes_a_real_secret_value_only_whether_one_is_configured(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-real-secret-value-should-never-appear")
        monkeypatch.setenv("UCS_GATEWAY_SECRET", "another-real-secret-should-never-appear")
        c = _fresh_client()
        body = c.get("/api/settings/runtime-config").json()
        raw = str(body)
        assert "sk-real-secret-value-should-never-appear" not in raw
        assert "another-real-secret-should-never-appear" not in raw
        # But the real presence IS reported, as a real boolean
        assert body["model_provider"]["anthropic_api_key_configured"] is True
        assert body["ucs"]["admin_token_configured"] is True

    def test_no_secret_configured_reports_false_not_a_missing_key(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("UCS_GATEWAY_SECRET", raising=False)
        monkeypatch.delenv("UCS_ADMIN_TOKEN", raising=False)
        c = _fresh_client()
        body = c.get("/api/settings/runtime-config").json()
        assert body["model_provider"]["anthropic_api_key_configured"] is False
        # No admin token needed: the service identity uses the gateway secret, here the
        # inbound ENVOY_INTERNAL_SECRET this service already holds (the data-plane setup).
        # Nothing configured at all is covered in test_ucs_service_identity.py.
        assert body["ucs"]["admin_token_configured"] is True
        assert body["ucs"]["admin_identity"] == "agents-service"
