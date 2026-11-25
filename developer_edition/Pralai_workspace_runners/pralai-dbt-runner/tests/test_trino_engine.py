"""Engine "trino": how dbt, SQL jobs, Python jobs and Kubernetes Jobs reach Trino.

A real Trino is covered by the live test in the bug log; here: the profile dbt gets, the two authentication modes (the warehouse JWT, and
"none" for a local Trino without authentication — dev only), the clear error when Trino is not configured, and what a Kubernetes Job gets.
"""
from __future__ import annotations

import sys
import types
import uuid

import pytest
import yaml

from app.core.config import Settings, get_settings
from app.executors import dbt_executor, isolation, sql_job_executor
from app.executors.python_job_executor import engine_environment
from tests.test_isolation import _env, _k8s_settings, no_real_k8s  # noqa: F401  (autouse: never a real kube config)


@pytest.fixture
def trino(monkeypatch):
    s = get_settings()
    for k, v in dict(trino_host="trino.internal", trino_port=8080, trino_catalog="iceberg", trino_http_scheme="http",
                     trino_auth_method="jwt", trino_user="inaiera").items():
        monkeypatch.setattr(s, k, v)
    return s


def _output(engine="trino"):
    return yaml.safe_load(dbt_executor._profiles_yml(engine))["inaiera"]["outputs"]["prod"]


def test_the_profile_points_dbt_at_the_configured_trino_with_the_warehouse_jwt(trino):
    out = _output()
    assert (out["type"], out["host"], out["port"], out["database"], out["http_scheme"]) == ("trino", "trino.internal", 8080, "iceberg", "http")
    assert out["method"] == "jwt" and out["jwt_token"] == "{{ env_var('TRINO_JWT', '') }}" and "user" not in out


def test_without_authentication_the_profile_sends_only_a_user(trino, monkeypatch):
    monkeypatch.setattr(trino, "trino_auth_method", "none")
    monkeypatch.setattr(trino, "trino_user", "analyst-1")
    out = _output()
    assert out["method"] == "none" and out["user"] == "analyst-1" and "jwt_token" not in out


def test_a_runner_without_trino_says_so_instead_of_writing_host_none(trino, monkeypatch):
    monkeypatch.setattr(trino, "trino_host", None)
    with pytest.raises(ValueError, match="TRINO_HOST"):
        dbt_executor._profiles_yml("trino")


def test_the_pg_lake_profile_is_unchanged_by_trino_settings(trino):
    assert _output("pg_lake")["type"] == "postgres"


@pytest.mark.parametrize("environment", ["staging", "prod"])
def test_trino_without_authentication_is_refused_outside_dev(environment):
    with pytest.raises(ValueError, match="TRINO_AUTH_METHOD=none"):
        Settings(environment=environment, trino_auth_method="none")
    assert Settings(environment="dev", trino_auth_method="none").trino_auth_method == "none"
    assert Settings(environment=environment).trino_auth_method == "jwt"


def _capture_connect(monkeypatch):
    seen = {}
    fake = types.SimpleNamespace(
        dbapi=types.SimpleNamespace(connect=lambda **kw: seen.update(kw) or "conn"),
        auth=types.SimpleNamespace(JWTAuthentication=lambda token: ("jwt", token)))
    monkeypatch.setitem(sys.modules, "trino", fake)
    return seen


def test_a_sql_job_uses_the_jwt_when_there_is_one(trino, monkeypatch):
    seen = _capture_connect(monkeypatch)
    monkeypatch.setenv("TRINO_JWT", "tok")
    sql_job_executor._trino_connect(trino)
    assert seen["auth"] == ("jwt", "tok") and seen["user"] == "inaiera-sql-job" and seen["http_scheme"] == "http"


def test_a_sql_job_without_authentication_sends_the_user_and_never_a_token(trino, monkeypatch):
    seen = _capture_connect(monkeypatch)
    monkeypatch.setattr(trino, "trino_auth_method", "none")
    monkeypatch.setenv("TRINO_JWT", "leftover")
    sql_job_executor._trino_connect(trino)
    assert seen["auth"] is None and seen["user"] == "inaiera"


def test_a_python_job_is_told_how_to_authenticate(trino, monkeypatch):
    monkeypatch.setenv("TRINO_JWT", "tok")
    env = engine_environment("trino")
    assert env["INAIERA_TRINO_AUTH"] == "jwt" and env["INAIERA_TRINO_JWT"] == "tok" and "INAIERA_TRINO_USER" not in env
    monkeypatch.setattr(trino, "trino_auth_method", "none")
    env = engine_environment("trino")
    assert env["INAIERA_TRINO_AUTH"] == "none" and env["INAIERA_TRINO_USER"] == "inaiera" and "INAIERA_TRINO_JWT" not in env


def _job_env(**kw):
    job = isolation._build_job(run_id=uuid.uuid4(), attempt=1, engine="trino", select=None, settings=_k8s_settings(**kw))
    return _env(job)


def test_a_kubernetes_job_gets_the_auth_mode_and_the_user():
    env = _job_env(trino_host="trino.svc", trino_auth_method="none", trino_user="dev-user")
    assert env["TRINO_AUTH_METHOD"].value == "none" and env["TRINO_USER"].value == "dev-user" and "TRINO_JWT" not in env


def test_a_kubernetes_job_in_dev_with_jwt_still_gets_the_secret():
    env = _job_env(trino_host="trino.svc", trino_jwt_secret_name="trino-jwt")
    assert env["TRINO_AUTH_METHOD"].value == "jwt" and env["TRINO_JWT"].value_from.secret_key_ref.name == "trino-jwt"


def test_a_kubernetes_job_without_authentication_is_not_given_a_leftover_jwt_secret():
    env = _job_env(trino_host="trino.svc", trino_auth_method="none", trino_jwt_secret_name="old-secret")
    assert "TRINO_JWT" not in env
