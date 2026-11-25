"""DQ-CFG-001: per-integration config, fail-fast startup, readiness."""
import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings, get_settings
from app.main import app, lifespan

GOOD = dict(
    ucs_url="http://ucs.svc", ucs_service_jwt_secret="u" * 40,
    dbtrunner_url="http://dbt.svc", dbtrunner_service_jwt_secret="d" * 40,
    flinksql_runner_url="http://flink.svc", flinksql_service_jwt_secret="f" * 40,
)


def _settings(**kw) -> Settings:
    return Settings(**{**GOOD, **kw})


def test_fully_configured_has_no_problems():
    assert _settings().integration_problems() == []


@pytest.mark.parametrize("field,env", [
    ("ucs_url", "UCS_URL"), ("ucs_service_jwt_secret", "UCS_SERVICE_JWT_SECRET"),
    ("dbtrunner_url", "DBTRUNNER_URL"),
    ("dbtrunner_service_jwt_secret", "DBTRUNNER_SERVICE_JWT_SECRET"),
    ("flinksql_runner_url", "FLINKSQL_RUNNER_URL"),
    ("flinksql_service_jwt_secret", "FLINKSQL_SERVICE_JWT_SECRET"),
])
def test_enabled_integration_missing_config_is_a_problem(field, env):
    problems = _settings(**{field: None}).integration_problems()
    assert any(env in p for p in problems), problems


@pytest.mark.parametrize("flag,field", [
    ("ucs_enabled", "ucs_url"), ("dbtrunner_enabled", "dbtrunner_service_jwt_secret"),
    ("flinksql_enabled", "flinksql_runner_url"),
])
def test_disabled_integration_needs_no_config(flag, field):
    assert _settings(**{flag: False, field: None}).integration_problems() == []


def test_no_default_urls_any_more(monkeypatch):
    for k in ("UCS_URL", "DBTRUNNER_URL", "FLINKSQL_RUNNER_URL"):
        monkeypatch.delenv(k, raising=False)
    s = Settings(_env_file=None)
    assert s.ucs_url is None and s.dbtrunner_url is None and s.flinksql_runner_url is None


def test_invalid_url_and_shared_secret_are_problems():
    p = _settings(dbtrunner_url="dbt:80").integration_problems()
    assert any("absolute http(s) URL" in x for x in p)
    p = _settings(flinksql_service_jwt_secret="u" * 40).integration_problems()
    assert any("different secrets" in x for x in p)


def test_short_secret_and_placeholder_org_rejected_outside_dev():
    p = _settings(environment="prod", org_id="local-dev",
                  ucs_service_jwt_secret="short").integration_problems()
    assert any("at least 32" in x for x in p) and any("ORG_ID" in x for x in p)


def test_dev_only_escape_hatches_refused_in_prod():
    p = _settings(environment="prod", org_id="org-1", legacy_internal_token_enabled=True,
                  ucs_bind_dev_bypass=True).integration_problems()
    assert any("LEGACY_INTERNAL_TOKEN_ENABLED" in x for x in p)
    assert any("UCS_BIND_DEV_BYPASS" in x for x in p)


def test_contract_version_mismatch_is_a_problem():
    p = _settings(quality_contract_version="1.0").integration_problems()
    assert any("QUALITY_CONTRACT_VERSION" in x for x in p)


def test_service_token_ttl_is_capped():
    with pytest.raises(ValueError):
        _settings(service_jwt_ttl_seconds=3600)


@pytest.fixture
def broken(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "dbtrunner_service_jwt_secret", None)
    return s


async def test_readyz_503_lists_every_problem(broken, monkeypatch):
    monkeypatch.setattr(broken, "ucs_url", None)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/readyz")
    assert r.status_code == 503
    problems = r.json()["problems"]
    assert any("DBTRUNNER_SERVICE_JWT_SECRET" in p for p in problems)
    assert any("UCS_URL" in p for p in problems)


async def test_startup_fails_when_misconfigured(broken):
    with pytest.raises(RuntimeError, match="DBTRUNNER_SERVICE_JWT_SECRET"):
        async with lifespan(app):
            pass


async def test_readyz_503_when_db_unreachable_even_if_configured(monkeypatch):
    import app.main as main

    class _Broken:
        async def __aenter__(self):
            raise ConnectionError("db down")

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(main, "AsyncSessionLocal", lambda: _Broken())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/readyz")
    assert r.status_code == 503 and r.json()["db"] == "unreachable"


async def test_readyz_ok_with_db(db, monkeypatch):
    from app.core import auth
    monkeypatch.setattr(auth.jwks_cache, "_keys", {"k": {}})
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/readyz")
    assert r.status_code == 200, r.text
    assert r.json()["db"] == "ok"
