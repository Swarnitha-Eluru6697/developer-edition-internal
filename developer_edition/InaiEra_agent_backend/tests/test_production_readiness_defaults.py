"""Production defaults that used to be advice in a comment.

Five findings from the V101.16.0 assessment, all the same shape: the
safe behaviour existed and was reachable, but only if an operator knew
to ask for it. A deployment that configured nothing inherited the
reference environment's settings and came up looking healthy.

  17  AUTO_MIGRATE defaulted true, so N replicas raced on `alembic
      upgrade head` at startup.
  19  Readiness asked about Anthropic only, so an OpenAI-only
      deployment reported not_configured.
  20  READYZ_REQUIRE defaulted to postgres alone, so a pod took
      traffic with Temporal down and no worker polling.
  21  /readyz handed any unauthenticated caller Temporal's address,
      the task queue name and raw exception text.
  22  CORS_ALLOWED_ORIGINS defaulted to `*`.

Each test states the production default AND that the explicit override
still works, because the fix is a change of default, not a new
prohibition — an operator who has read the options keeps every one of
them.
"""
import importlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import app.health as health_module
from app.environment import env_flag, is_production


@pytest.fixture
def production(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")


@pytest.fixture
def no_readyz_require(monkeypatch):
    """The unset case. This container sets nothing, but a developer's
    shell might, and a test that silently reads an ambient value is a
    test that passes for the wrong reason."""
    monkeypatch.delenv("READYZ_REQUIRE", raising=False)


class TestSharedProductionPredicate:
    """One definition of "production". There were three, and two of the
    three carried a comment warning they must not drift."""

    def test_production_is_recognised(self, production):
        assert is_production() is True

    @pytest.mark.parametrize("value", ["", "dev", "staging", "Production "])
    def test_only_the_exact_token_counts(self, monkeypatch, value):
        monkeypatch.setenv("ENVIRONMENT", value)
        # " Production " with whitespace and capitals DOES count: the
        # existing three copies all stripped and lowercased, and
        # changing that would alter behaviour for deployments relying
        # on it. Everything else must not.
        assert is_production() is (value.strip().lower() == "production")

    def test_unset_is_not_production(self, monkeypatch):
        monkeypatch.delenv("ENVIRONMENT", raising=False)
        assert is_production() is False


class TestEnvFlagRefusesToGuess:
    @pytest.mark.parametrize("raw,expected", [
        ("true", True), ("TRUE", True), ("1", True), ("yes", True), ("on", True),
        ("false", False), ("0", False), ("no", False), ("off", False),
    ])
    def test_accepts_the_spellings_operators_type(self, monkeypatch, raw, expected):
        monkeypatch.setenv("FLAG_UNDER_TEST", raw)
        assert env_flag("FLAG_UNDER_TEST", default=not expected) is expected

    def test_unset_takes_the_default(self, monkeypatch):
        monkeypatch.delenv("FLAG_UNDER_TEST", raising=False)
        assert env_flag("FLAG_UNDER_TEST", default=True) is True
        assert env_flag("FLAG_UNDER_TEST", default=False) is False

    def test_an_unrecognised_value_raises_rather_than_falling_back(self, monkeypatch):
        # The failure this prevents: AUTO_MIGRATE=disabled silently
        # evaluating to the default (true, historically) and migrating
        # anyway — the operator asked for the opposite of what they got.
        monkeypatch.setenv("FLAG_UNDER_TEST", "disabled")
        with pytest.raises(RuntimeError, match="not a boolean"):
            env_flag("FLAG_UNDER_TEST", default=False)


class TestAutoMigrateDefault:
    """Finding 17."""

    def test_defaults_on_outside_production(self, monkeypatch):
        monkeypatch.delenv("ENVIRONMENT", raising=False)
        monkeypatch.delenv("AUTO_MIGRATE", raising=False)
        assert env_flag("AUTO_MIGRATE", default=not is_production()) is True

    def test_defaults_off_in_production(self, production, monkeypatch):
        monkeypatch.delenv("AUTO_MIGRATE", raising=False)
        assert env_flag("AUTO_MIGRATE", default=not is_production()) is False

    def test_production_can_still_opt_in(self, production, monkeypatch):
        # A single-replica production deployment has no race to lose.
        monkeypatch.setenv("AUTO_MIGRATE", "true")
        assert env_flag("AUTO_MIGRATE", default=not is_production()) is True

    def test_init_db_does_not_migrate_in_production_by_default(self, production, monkeypatch):
        """The wiring, not just the predicate.

        Asserted by watching whether _run_alembic_upgrade is reached,
        because the bug was never in the boolean — it was in which
        default that boolean got.
        """
        monkeypatch.delenv("AUTO_MIGRATE", raising=False)
        import app.db as db_module

        called = []
        monkeypatch.setattr(db_module, "_run_alembic_upgrade", lambda: called.append(1))
        db_module.init_db()
        assert called == [], "production startup ran migrations"

        monkeypatch.setenv("AUTO_MIGRATE", "true")
        db_module.init_db()
        assert called == [1], "explicit AUTO_MIGRATE=true was ignored"


class TestRequiredDependencies:
    """Finding 20."""

    def test_production_requires_the_execution_path_not_just_postgres(
            self, production, no_readyz_require):
        required = health_module._required_dependencies()
        assert required == {"postgres", "temporal", "worker", "model_provider"}

    def test_non_production_still_requires_postgres_alone(self, monkeypatch, no_readyz_require):
        monkeypatch.delenv("ENVIRONMENT", raising=False)
        assert health_module._required_dependencies() == {"postgres"}

    def test_ucs_is_not_required_in_production(self, production, no_readyz_require):
        # An install with no catalogue wired up is supported, so gating
        # on it would take working deployments out of rotation.
        assert "ucs" not in health_module._required_dependencies()

    def test_an_explicit_list_wins_even_when_it_narrows_production(
            self, production, monkeypatch):
        monkeypatch.setenv("READYZ_REQUIRE", "postgres")
        assert health_module._required_dependencies() == {"postgres"}

    def test_a_misspelled_dependency_is_refused_not_ignored(self, monkeypatch):
        # Ignoring it means requiring postgres alone while the
        # deployment believes it is gating on Temporal.
        monkeypatch.setenv("READYZ_REQUIRE", "postgres,temportal")
        with pytest.raises(RuntimeError, match="temportal"):
            health_module._required_dependencies()

    def test_every_default_name_is_a_real_check(self, production, no_readyz_require):
        """A required name with no check behind it can never be ok, so
        the pod would never become ready. Guards against a rename."""
        import asyncio

        report = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            health_module._readiness_report())
        for name in health_module._required_dependencies():
            assert name in report["checks"], f"{name} is required but never checked"


class TestModelProviderIsNotAnthropicOnly:
    """Finding 19."""

    def test_an_openai_only_deployment_is_configured(self, monkeypatch, no_stored_provider_key):
        no_stored_provider_key("Anthropic")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-fake-for-this-test")
        result = health_module._check_model_provider()
        assert result["status"] == "ok"
        assert "OpenAI" in result["providers"]

    def test_anthropic_still_counts(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake-for-this-test")
        result = health_module._check_model_provider()
        assert result["status"] == "ok"
        assert "Anthropic" in result["providers"]

    def test_nothing_configured_is_reported_honestly(
            self, monkeypatch, no_stored_provider_key):
        for name in sorted(_runtime_providers()):
            no_stored_provider_key(name)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setenv("ALLOW_MOCK_MODELS", "true")
        result = health_module._check_model_provider()
        assert result["status"] == "not_configured"
        assert "mock" in result["detail"].lower()

    def test_a_named_required_provider_is_the_only_one_that_counts(
            self, monkeypatch, no_stored_provider_key):
        """Production policy picks a provider; another one's key must
        not satisfy it. Otherwise a deployment that requires OpenAI
        reports ready on a leftover Anthropic key."""
        no_stored_provider_key("OpenAI")
        monkeypatch.setenv("MODEL_PROVIDER_REQUIRED", "OpenAI")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake-for-this-test")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        assert health_module._check_model_provider()["status"] == "not_configured"

        monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-fake-for-this-test")
        result = health_module._check_model_provider()
        assert result["status"] == "ok"
        assert result["required"] == "OpenAI"

    def test_requiring_a_provider_with_no_adapter_fails_readiness(self, monkeypatch):
        # Not "not_configured": nothing an operator adds to the
        # Integrations page will make Amazon Bedrock executable, so
        # reporting a missing key would send them after the wrong thing.
        monkeypatch.setenv("MODEL_PROVIDER_REQUIRED", "Amazon Bedrock")
        result = health_module._check_model_provider()
        assert result["status"] == "unreachable"
        assert "no runtime adapter" in result["detail"]


def _runtime_providers():
    from app.model_adapter import RUNTIME_SUPPORTED_PROVIDERS
    return RUNTIME_SUPPORTED_PROVIDERS


class TestReadyzDoesNotLeakTopology:
    """Finding 21."""

    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient
        from app.main import app
        return TestClient(app)

    def test_the_public_probe_omits_addresses_queues_and_exception_text(self, client):
        body = client.get("/readyz").json()
        leaked = []
        for name, check in body["checks"].items():
            for key in ("address", "task_queue", "url", "http_status",
                        "detail", "poller_count", "engine", "providers"):
                if key in check:
                    leaked.append(f"{name}.{key}")
        assert not leaked, f"/readyz disclosed {leaked}"

    def test_provider_names_are_a_count_publicly_and_a_list_internally(self, client):
        """Which vendors this deployment holds keys for is a map of its
        supply chain. The count is the part readiness asserts."""
        import asyncio

        public = client.get("/readyz").json()["checks"]["model_provider"]
        assert "providers" not in public
        if public["status"] == "ok":
            assert public["providers_configured"] >= 1

        internal = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            health_module._readiness_report())
        if internal["checks"]["model_provider"]["status"] == "ok":
            assert isinstance(internal["checks"]["model_provider"]["providers"], list)

    def test_it_still_says_which_component_is_failing(self, client):
        # Stripping detail must not turn the probe into a bare
        # yes/no — an operator needs to know WHICH dependency is down.
        body = client.get("/readyz").json()
        assert set(body["checks"]) >= {"postgres", "temporal", "worker",
                                       "ucs", "model_provider"}
        assert all("status" in c for c in body["checks"].values())
        assert "required" in body

    def test_the_scanner_mode_survives_because_it_is_policy_not_topology(self, client):
        # The malware_scanner work exists to make heuristic-only
        # scanning visible. Hiding `mode` here would undo it.
        scanner = client.get("/readyz").json()["checks"]["malware_scanner"]
        assert "mode" in scanner

    @pytest.mark.asyncio
    async def test_the_internal_view_keeps_the_detail(self):
        report = await health_module.readyz_detail()
        assert report["checks"]["temporal"].get("address"), \
            "the diagnostic view lost the detail it exists to provide"
        assert "failed_required" in report

    def test_the_internal_route_is_operator_gated_and_unpublished(self, client):
        # Same three layers as every other /internal route: the prefix
        # an ingress refuses, require_operator failing closed, and no
        # OpenAPI entry inviting a client to depend on it.
        from fastapi.testclient import TestClient
        from app.main import app
        from conftest import ALL_PERMISSIONS, gateway_headers
        non_operator = gateway_headers([p for p in ALL_PERMISSIONS if p != "agent:maintenance:run"])
        assert client.get("/internal/diagnostics/readyz", headers=non_operator).status_code in (401, 403)
        assert TestClient(app, headers={}).get("/internal/diagnostics/readyz").status_code == 401
        assert "/internal/diagnostics/readyz" not in client.get("/openapi.json").json()["paths"]

    def test_both_surfaces_agree_on_the_verdict(self, client):
        """Two probe paths is two answers waiting to happen."""
        import asyncio

        public = client.get("/readyz").json()
        internal = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            health_module._readiness_report())
        assert public["status"] == internal["status"]
        assert public["required"] == internal["required"]


class TestCorsRequiresAnAllowlistInProduction:
    """Finding 22."""

    def _reload_main(self):
        import app.main as main_module
        return importlib.reload(main_module)

    def test_wildcard_is_still_the_default_outside_production(self, monkeypatch):
        monkeypatch.delenv("ENVIRONMENT", raising=False)
        monkeypatch.delenv("CORS_ALLOWED_ORIGINS", raising=False)
        from app.main import _cors_allowed_origins
        assert _cors_allowed_origins() == ["*"]

    def test_production_with_no_allowlist_refuses_to_start(self, production, monkeypatch):
        monkeypatch.delenv("CORS_ALLOWED_ORIGINS", raising=False)
        from app.main import _cors_allowed_origins
        with pytest.raises(RuntimeError, match="CORS_ALLOWED_ORIGINS"):
            _cors_allowed_origins()

    def test_production_refuses_an_explicit_wildcard_too(self, production, monkeypatch):
        # Setting it to "*" is not a decision to allow every origin so
        # much as a way to satisfy a checklist.
        monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://app.example.com,*")
        from app.main import _cors_allowed_origins
        with pytest.raises(RuntimeError, match="wildcard"):
            _cors_allowed_origins()

    def test_production_refuses_a_set_but_empty_allowlist(self, production, monkeypatch):
        monkeypatch.setenv("CORS_ALLOWED_ORIGINS", " , , ")
        from app.main import _cors_allowed_origins
        with pytest.raises(RuntimeError, match="names no origins"):
            _cors_allowed_origins()

    def test_production_accepts_an_explicit_list(self, production, monkeypatch):
        monkeypatch.setenv("CORS_ALLOWED_ORIGINS",
                           "https://app.example.com, https://admin.example.com")
        from app.main import _cors_allowed_origins
        assert _cors_allowed_origins() == ["https://app.example.com",
                                           "https://admin.example.com"]

    def test_the_refusal_happens_at_import_so_no_request_is_ever_served(
            self, production, monkeypatch):
        """A warning would let the pod come up and serve cross-origin
        requests from anywhere for its whole life."""
        monkeypatch.delenv("CORS_ALLOWED_ORIGINS", raising=False)
        with pytest.raises(RuntimeError, match="CORS_ALLOWED_ORIGINS"):
            self._reload_main()
        # Leave the module importable for whatever runs next.
        monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "http://localhost:3000")
        monkeypatch.delenv("ENVIRONMENT", raising=False)
        self._reload_main()
