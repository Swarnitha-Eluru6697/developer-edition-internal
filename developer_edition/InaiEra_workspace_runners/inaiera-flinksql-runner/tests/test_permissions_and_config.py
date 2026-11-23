"""RUN-FLINK-009 — permissions on mutating routes, org-id startup guard, and
RUN-FLINK-016 env-name wiring in Settings."""
from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.core.auth import Principal
from app.core.config import Settings
from app.core.deps import get_current_user
from app.core.permissions import PERMISSIONS, SCOPE_PERMISSIONS, has_permission
from app.main import app
from tests.perms import EDITOR, ENGINEER, VIEWER

# These tests are about PERMISSIONS, so the principal carries every route scope (scope enforcement itself
# is covered in tests/test_delegated_auth.py).
ALL_SCOPES = ["flink.jobs.read", "flink.jobs.edit", "flink.jobs.deploy", "flink.catalog.read"]


def _p(*permissions: str) -> Principal:
    return Principal(user_id="u1", org_id="local-dev", permissions=frozenset(permissions), scopes=ALL_SCOPES)


def test_the_table_names_only_workspace_permissions():
    assert PERMISSIONS == frozenset(ENGINEER)
    assert all(p.startswith("workspace:") for p in PERMISSIONS)
    assert set(SCOPE_PERMISSIONS) == set(ALL_SCOPES)


@pytest.mark.parametrize("held,perm,expected", [
    (ENGINEER, "workspace:flink:deploy", True),
    (["workspace:flink:deploy"], "workspace:flink:deploy", True),
    (EDITOR, "workspace:flink:deploy", False),
    ([], "workspace:catalog:view", False),
    (["workspace:flink"], "workspace:flink:edit", False),          # exact IDs only
    (["WORKSPACE:FLINK:EDIT"], "workspace:flink:edit", False),
])
def test_has_permission(held, perm, expected):
    assert has_permission(_p(*held), perm) is expected


# ── route enforcement ─────────────────────────────────────────────────────
JOB = str(uuid.uuid4())
MUTATING = [
    ("post", "/api/v1/jobs", {"name": "j", "sql": "INSERT INTO t SELECT 1"}),
    ("patch", f"/api/v1/jobs/{JOB}", {"description": "x"}),
    ("delete", f"/api/v1/jobs/{JOB}", None),
    ("post", f"/api/v1/jobs/{JOB}/validate", None),
    ("post", f"/api/v1/jobs/{JOB}/deploy", {}),
    ("post", f"/api/v1/jobs/{JOB}/stop", {}),
    ("post", f"/api/v1/jobs/{JOB}/cancel", None),
    ("post", f"/api/v1/jobs/{JOB}/savepoint", None),
]


@pytest.fixture
def as_user(client):
    def _set(*permissions):
        app.dependency_overrides[get_current_user] = lambda: _p(*permissions)
    yield _set
    app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.parametrize("method,path,body", MUTATING)
def test_a_viewer_cannot_mutate_jobs(client, as_user, method, path, body):
    as_user(*VIEWER)
    kw = {"json": body} if body is not None else {}
    resp = getattr(client, method)(path, **kw)
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "FORBIDDEN"


@pytest.mark.parametrize("method,path,body", MUTATING)
def test_an_engineer_passes_the_permission_gate(client, as_user, method, path, body):
    as_user(*ENGINEER)
    kw = {"json": body} if body is not None else {}
    resp = getattr(client, method)(path, **kw)
    assert resp.status_code not in (401, 403)


def test_token_without_permissions_cannot_even_list(client, as_user):
    as_user()
    assert client.get("/api/v1/jobs").status_code == 403


def test_kafka_preview_requires_its_permission(client, as_user):
    as_user("workspace:catalog:view", "workspace:flink:deploy")
    assert client.get("/api/v1/kafka/topics/orders/preview").status_code == 403


@pytest.mark.parametrize("role_claim", ["admin", "workspace_admin"])
def test_a_role_name_grants_nothing(client, role_claim):
    p = _p()
    p.__dict__["roles"] = [role_claim]                            # even if something attached one, it is never read
    app.dependency_overrides[get_current_user] = lambda: p
    try:
        assert client.post(f"/api/v1/jobs/{JOB}/deploy", json={}).status_code == 403
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# ── RUN-FLINK-009: placeholder org refused outside dev ────────────────────
@pytest.mark.parametrize("org", ["local-dev", "REPLACE_ME", ""])
@pytest.mark.parametrize("env", ["staging", "prod"])
def test_placeholder_org_id_fails_startup_outside_dev(org, env):
    with pytest.raises(ValidationError, match="ORG_ID"):
        Settings(environment=env, org_id=org)


def test_placeholder_org_id_is_fine_in_dev_and_real_org_fine_in_prod():
    assert Settings(environment="dev", org_id="local-dev").org_id == "local-dev"
    assert Settings(environment="prod", org_id="acme-42").org_id == "acme-42"


# ── RUN-FLINK-016: credential env names ───────────────────────────────────
@pytest.mark.parametrize("user_var,pass_var", [
    ("KAFKA_SASL_USERNAME", "KAFKA_SASL_PASSWORD"),
    ("KAFKA_USERNAME", "KAFKA_PASSWORD"),
])
def test_kafka_credentials_are_read_from_either_env_spelling(monkeypatch, user_var, pass_var):
    monkeypatch.setenv(user_var, "svc")
    monkeypatch.setenv(pass_var, "pw")
    s = Settings()
    assert (s.kafka_sasl_username, s.kafka_sasl_password) == ("svc", "pw")


def test_environment_and_rest_proxy_url_come_from_env(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "prod")
    monkeypatch.setenv("ORG_ID", "acme")
    monkeypatch.setenv("KAFKA_REST_PROXY_URL", "https://proxy:8443")
    monkeypatch.setenv("KAFKA_TOPIC_ALLOWED_PATTERNS", '["{org_id}.*"]')
    s = Settings()
    assert s.environment == "prod"
    assert s.kafka_rest_proxy_url == "https://proxy:8443"
    assert s.kafka_topic_allowed_patterns == ["{org_id}.*"]
    assert s.legacy_internal_token_allowed is False
