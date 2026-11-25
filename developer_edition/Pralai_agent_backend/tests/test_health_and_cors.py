"""
Real tests for the health/readiness and CORS configuration fixes
(section 0.81, GAP_REPORT).

The real, reported bugs: /api/health always returned {"status": "ok"},
checking nothing real. CORS was hardcoded to allow_origins=["*"].

The fix: real, separate /livez (no dependency checks) and /readyz
(genuinely checks Postgres/Temporal/worker/UCS/model-provider, each
independently timed out). CORS_ALLOWED_ORIGINS makes the CORS policy a
real, deployment-time configuration choice, defaulting to the existing
"*" so nothing changes for this environment unless explicitly
configured.

These tests exercise the real, running app and real health-check
functions directly, using this sandbox's own real, genuinely
unreachable Temporal/UCS to prove the checks report honest failures,
not fabricated successes.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

from app.main import app
import app.health as health_module

client = TestClient(app)


class TestRealLivez:
    def test_livez_responds_without_checking_any_dependency(self):
        r = client.get("/livez")
        assert r.status_code == 200
        assert r.json() == {"status": "alive"}


class TestRealReadyzIndividualChecks:
    @pytest.mark.asyncio
    async def test_postgres_check_is_genuinely_ok_against_the_real_db(self):
        result = await health_module._check_postgres()
        assert result["status"] == "ok"

    # These three originally asserted status == "unreachable", which was
    # true of the sandbox they were written in and is not a property of
    # the check. Run anywhere Temporal and UCS are actually up — this
    # compose stack, for one — they failed while the code was correct.
    # What the checks genuinely promise is a known status, and a reason
    # whenever they could not reach the thing. That is what is asserted
    # now, in both directions.
    @pytest.mark.asyncio
    async def test_temporal_check_reports_a_known_status_with_a_reason_on_failure(self):
        result = await health_module._check_temporal()
        assert result["status"] in ("ok", "unreachable")
        if result["status"] == "unreachable":
            assert result.get("detail"), "unreachable without saying why"
        else:
            assert result.get("address"), "reachable without saying what it reached"

    @pytest.mark.asyncio
    async def test_worker_check_distinguishes_a_polling_worker_from_none(self):
        result = await health_module._check_worker()
        assert result["status"] in ("ok", "unreachable")
        if result["status"] == "ok":
            # The point of this check over a plain Temporal ping: a
            # reachable server with nobody polling is not a working
            # durable path.
            assert result.get("poller_count", 0) >= 1

    @pytest.mark.asyncio
    async def test_ucs_check_counts_any_http_response_as_reachable(self):
        result = await health_module._check_ucs()
        assert result["status"] in ("ok", "unreachable")
        if result["status"] == "ok":
            # Even a 404 counts — the question is process reachability,
            # not a specific endpoint's contract.
            assert result.get("http_status")

    def test_model_provider_check_reports_configuration_state_honestly(
            self, monkeypatch, no_stored_provider_key):
        # Deleting the env var is not enough to make this deployment
        # unconfigured: the check also reads the provider rows the
        # Integrations page writes, and this database has keys in them.
        # Without clearing those too, the test asserted "not_configured"
        # against an install that genuinely was configured.
        from app.model_adapter import RUNTIME_SUPPORTED_PROVIDERS

        for name in sorted(RUNTIME_SUPPORTED_PROVIDERS):
            no_stored_provider_key(name)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setenv("ALLOW_MOCK_MODELS", "true")
        result = health_module._check_model_provider()
        assert result["status"] == "not_configured"
        assert "mock" in result["detail"].lower()

    def test_model_provider_check_is_ok_when_a_real_key_is_present(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake-for-this-test")
        result = health_module._check_model_provider()
        assert result["status"] == "ok"
        # Reports every configured provider now, not just Anthropic —
        # see test_production_readiness_defaults.py for why.
        assert "Anthropic" in result["providers"]


class TestRealReadyzEndpoint:
    def test_readyz_reports_every_named_dependency(self):
        r = client.get("/readyz")
        body = r.json()
        for dep in ("postgres", "temporal", "worker", "ucs", "model_provider"):
            assert dep in body["checks"], f"missing real check for {dep}"

    def test_readyz_defaults_to_only_requiring_postgres_outside_production(self, monkeypatch):
        # Still the default here. Under ENVIRONMENT=production it is now
        # postgres, temporal, worker and model_provider — see
        # test_production_readiness_defaults.py.
        monkeypatch.delenv("ENVIRONMENT", raising=False)
        monkeypatch.delenv("READYZ_REQUIRE", raising=False)
        assert client.get("/readyz").json()["required"] == ["postgres"]

    def test_readyz_returns_200_when_only_postgres_is_required_and_it_is_ok(self):
        r = client.get("/readyz")
        assert r.status_code == 200
        assert r.json()["status"] == "ready"

    def test_readyz_returns_503_when_a_required_dependency_is_genuinely_down(self, monkeypatch):
        # Originally this just required Temporal and relied on Temporal
        # being down. Where it is up, the assertion failed even though
        # the gating logic was right. The dependency is now made to fail
        # explicitly, so the test is about the gate.
        async def down():
            return {"status": "unreachable", "detail": "forced down for this test"}

        monkeypatch.setattr(health_module, "_check_temporal", down)
        monkeypatch.setenv("READYZ_REQUIRE", "postgres,temporal")
        r = client.get("/readyz")
        assert r.status_code == 503
        assert r.json()["status"] == "not_ready"
        assert "temporal" in r.json()["required"]
        # And the reason stays off the public probe.
        assert "detail" not in r.json()["checks"]["temporal"]


class TestRealBackwardCompatHealthAlias:
    def test_api_health_still_responds_and_matches_livez(self):
        r = client.get("/api/health")
        assert r.status_code == 200
        assert r.json() == {"status": "alive"}


class TestRealConfigurableCors:
    def test_default_cors_still_allows_any_origin(self, monkeypatch):
        # Asserted against the resolver rather than a live response:
        # this compose stack sets CORS_ALLOWED_ORIGINS, so the running
        # app correctly does NOT answer "*" and the original assertion
        # failed on a correct deployment. The middleware is built once at
        # import, so a request cannot show the default without a reload.
        monkeypatch.delenv("ENVIRONMENT", raising=False)
        monkeypatch.delenv("CORS_ALLOWED_ORIGINS", raising=False)
        from app.main import _cors_allowed_origins

        assert _cors_allowed_origins() == ["*"]

    def test_cors_origins_are_read_from_the_real_env_var(self):
        import importlib
        import app.main as main_module

        os.environ["CORS_ALLOWED_ORIGINS"] = "https://only-this-origin.example.com"
        try:
            importlib.reload(main_module)
            fresh_client = TestClient(main_module.app)
            r = fresh_client.get("/api/agents", headers={"Origin": "https://only-this-origin.example.com"})
            assert r.headers.get("access-control-allow-origin") == "https://only-this-origin.example.com"

            r2 = fresh_client.get("/api/agents", headers={"Origin": "https://not-this-one.example.com"})
            assert r2.headers.get("access-control-allow-origin") is None
        finally:
            del os.environ["CORS_ALLOWED_ORIGINS"]
            importlib.reload(main_module)
