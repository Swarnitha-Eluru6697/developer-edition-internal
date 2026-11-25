"""Startup guards that must fail closed outside dev (WS-RUNTIME-012).

These call the guard function directly against a monkeypatched get_settings
so they exercise the actual validation logic without booting the app or a
real database — mirrors the plain-function unit-test style already used in
tests/test_storage_keys.py.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.executors.local_kernel_executor import assert_local_kernel_safe_for_environment


def _settings(*, local_python_kernel_enabled: bool, environment: str) -> SimpleNamespace:
    return SimpleNamespace(
        local_python_kernel_enabled=local_python_kernel_enabled,
        environment=environment,
    )


def test_local_kernel_enabled_in_dev_is_allowed(monkeypatch):
    monkeypatch.setattr(
        "app.executors.local_kernel_executor.get_settings",
        lambda: _settings(local_python_kernel_enabled=True, environment="dev"),
    )
    assert_local_kernel_safe_for_environment()  # must not raise


@pytest.mark.parametrize("env", ["staging", "prod"])
def test_local_kernel_enabled_outside_dev_fails_startup(monkeypatch, env):
    monkeypatch.setattr(
        "app.executors.local_kernel_executor.get_settings",
        lambda: _settings(local_python_kernel_enabled=True, environment=env),
    )
    with pytest.raises(RuntimeError, match="local_python_kernel_enabled"):
        assert_local_kernel_safe_for_environment()


@pytest.mark.parametrize("env", ["staging", "prod"])
def test_local_kernel_disabled_outside_dev_is_allowed(monkeypatch, env):
    monkeypatch.setattr(
        "app.executors.local_kernel_executor.get_settings",
        lambda: _settings(local_python_kernel_enabled=False, environment=env),
    )
    assert_local_kernel_safe_for_environment()  # must not raise


# ── Secrets backend guard (WS-SEC-002) ──────────────────────────────────

from app.storage.secrets import assert_secrets_backend_safe_for_environment  # noqa: E402


@pytest.mark.parametrize("env", ["staging", "prod"])
def test_local_secrets_backend_outside_dev_fails_startup(monkeypatch, env):
    monkeypatch.setattr(
        "app.storage.secrets.get_settings",
        lambda: SimpleNamespace(secrets_backend="local", environment=env),
    )
    with pytest.raises(RuntimeError, match="secrets_backend=local"):
        assert_secrets_backend_safe_for_environment()


@pytest.mark.parametrize("backend", ["aws_secrets_manager", "azure_key_vault", "gcp_secret_manager"])
def test_cloud_secrets_backend_in_prod_is_allowed(monkeypatch, backend):
    monkeypatch.setattr(
        "app.storage.secrets.get_settings",
        lambda: SimpleNamespace(secrets_backend=backend, environment="prod"),
    )
    assert_secrets_backend_safe_for_environment()  # must not raise


def test_local_secrets_backend_in_dev_is_allowed(monkeypatch):
    monkeypatch.setattr(
        "app.storage.secrets.get_settings",
        lambda: SimpleNamespace(secrets_backend="local", environment="dev"),
    )
    assert_secrets_backend_safe_for_environment()  # must not raise


# ── DATABASE_PASSWORD merge (WS-CFG-004: Helm can't expand it in envFrom) ──

def test_database_password_is_merged_url_encoded():
    from sqlalchemy.engine import make_url
    from app.core.config import Settings
    pw = "p@ss:w/rd#%1"
    s = Settings(
        database_url="postgresql+asyncpg://workspace@db.svc:5432/workspace?sslmode=require",
        database_password=pw,
    )
    url = make_url(s.database_url)
    assert url.password == pw            # round-trips despite @ : / # %
    assert url.username == "workspace" and url.host == "db.svc" and url.database == "workspace"
    assert url.query == {"sslmode": "require"}


def test_database_url_unchanged_without_password():
    from app.core.config import Settings
    url = "postgresql+asyncpg://workspace:workspace@localhost:5432/workspace"
    assert Settings(database_url=url, database_password=None).database_url == url


# ── Governance profile guard (W-010) ─────────────────────────────────────
from app.services.ucs_client import assert_governance_for_environment  # noqa: E402


def _gov(env, profile, url="http://ucs", secret="s"):
    return SimpleNamespace(environment=env, governance_profile=profile, ucs_service_url=url, ucs_gateway_secret=secret)


@pytest.mark.parametrize("env", ["staging", "prod"])
@pytest.mark.parametrize("url,secret,missing", [(None, "s", "UCS_SERVICE_URL"), ("http://ucs", None, "UCS_GATEWAY_SECRET"), ("", "", "UCS_SERVICE_URL")])
def test_production_governed_without_ucs_fails_startup(monkeypatch, env, url, secret, missing):
    monkeypatch.setattr("app.services.ucs_client.get_settings", lambda: _gov(env, "production-governed", url, secret))
    with pytest.raises(RuntimeError, match=missing):
        assert_governance_for_environment()


@pytest.mark.parametrize("env", ["staging", "prod"])
def test_production_governed_with_ucs_starts(monkeypatch, env):
    monkeypatch.setattr("app.services.ucs_client.get_settings", lambda: _gov(env, "production-governed"))
    assert_governance_for_environment()


def test_dev_never_needs_ucs(monkeypatch):
    monkeypatch.setattr("app.services.ucs_client.get_settings", lambda: _gov("dev", "production-governed", None, None))
    assert_governance_for_environment()


def test_the_ungoverned_profile_starts_but_is_logged_as_unsupported(monkeypatch):
    from app.services import ucs_client
    seen = []
    monkeypatch.setattr(ucs_client, "get_settings", lambda: _gov("prod", "development-ungoverned", None, None))
    monkeypatch.setattr(ucs_client.log, "critical", lambda event, **kw: seen.append(event))
    assert_governance_for_environment()
    assert seen == ["governance.ungoverned_profile_unsupported_for_production"]


def test_the_default_profile_is_production_governed():
    from app.core.config import Settings
    assert Settings().governance_profile == "production-governed"
