"""Pytest fixtures for the Workspace service.

Gate 1 (InaiEra Gateway Auth): identity arrives as Envoy-injected, trusted
headers, not a JWT this service validates itself (see app/core/deps.py).
There is no Envoy in tests, so these fixtures play Envoy: they set the two
env vars the gateway_auth core primitives require (TRUST_ENVOY_HEADERS,
ENVOY_INTERNAL_SECRET) and hand tests a factory for the headers Envoy would
have injected.

For tests that need a database, we point at a sqlite-backed async engine and
let SQLAlchemy migrate the models. Postgres-only features (jsonb, specific
types) are wrapped at the model level so this works.
"""
from __future__ import annotations

import os

import pytest

# Set env BEFORE importing the app so Settings/get_current_user pick it up
os.environ.setdefault("ENVIRONMENT", "dev")
os.environ.setdefault("ORG_ID", "00000000-0000-0000-0000-000000000001")
# Integration tests (marker `integration`) run against a real, migrated
# Postgres when INTEGRATION_DATABASE_URL is set; otherwise they're skipped
# and everything else uses SQLite/fakes.
INTEGRATION_DATABASE_URL = os.environ.get("INTEGRATION_DATABASE_URL")
os.environ.setdefault("DATABASE_URL", INTEGRATION_DATABASE_URL or "sqlite+aiosqlite:///:memory:")
if INTEGRATION_DATABASE_URL:
    # Each TestClient runs its own event loop; pooled asyncpg connections
    # can't cross loops.
    os.environ.setdefault("DATABASE_NULL_POOL", "true")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")
os.environ.setdefault("STORAGE_BACKEND", "local")
# The alert sweep is a background loop: tests that start the app (lifespan) must not run it against their in-memory database.
os.environ.setdefault("ALERTS_SWEEP_ENABLED", "false")
os.environ.setdefault("TRUST_ENVOY_HEADERS", "true")
os.environ.setdefault("ENVOY_INTERNAL_SECRET", "test-gateway-secret")

import uuid  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

TEST_ORG_ID = "00000000-0000-0000-0000-000000000001"
TEST_GATEWAY_SECRET = "test-gateway-secret"


def subject_for(user_name: str) -> str:
    """The control-plane subject (UUID) the test gateway injects for a user name (W-020: identity is the subject)."""
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{user_name}.test.inaiera"))


def encode_list(values) -> str:
    """How Envoy renders a list claim into a header: base64 of the JSON array (contract section 4)."""
    import base64
    import json
    return base64.b64encode(json.dumps(list(values)).encode()).decode()


def _headers(
    *, user_name: str | None = "test-user", roles: list[str] | None = None,
    org_id: str | None = TEST_ORG_ID, groups: list[str] | None = None,
    secret: str | None = TEST_GATEWAY_SECRET, user_id: str | None = "auto",
    permissions: list[str] | None = None,
) -> dict[str, str]:
    """`user_id`: "auto" = the subject derived from `user_name` (what a real gateway injects as x-user-id); None = the
    header is omitted (an older gateway).

    `permissions`: exactly what x-user-permissions carries. When omitted, it is derived from `roles` through the TEST seed
    (tests/role_seed.py), the way the Admin Portal would have resolved those roles; the app itself never sees role names
    except in x-user-roles, which it only keeps for display/audit."""
    from role_seed import permissions_for

    headers: dict[str, str] = {}
    if secret is not None:
        headers["X-Inaiera-Gateway-Secret"] = secret
    if user_name is not None:
        headers["X-User-Name"] = user_name
        if user_id == "auto":
            user_id = subject_for(user_name)
    if user_id is not None:
        headers["X-User-Id"] = user_id
    if org_id is not None:
        headers["X-Tenant-Id"] = org_id
    roles = roles if roles is not None else ["developer"]
    headers["X-User-Roles"] = ",".join(roles)
    headers["X-User-Permissions"] = encode_list(permissions if permissions is not None else permissions_for(roles))
    if groups:
        headers["X-User-Groups"] = ",".join(groups)
    return headers


@pytest.fixture
def gateway_headers():
    """Factory fixture: tests call gateway_headers(roles=[...]) to get the
    trusted headers Envoy would inject for a request through Gate 1.

    Defaults to a valid, correctly-secreted request with role "developer" —
    "authenticated, may open Workspace and edit files, nothing else"
    (tests/role_seed.py BASE; see tests/test_rbac.py).
    """
    return _headers


@pytest.fixture
def auth_headers() -> dict[str, str]:
    """A minimal valid, correctly-secreted request — same shape/name as the
    pre-Gate-1 fixture so tests that only need SOME authenticated caller
    (tests/test_workspaces.py) didn't need to change at all."""
    return _headers()


@pytest.fixture
def client() -> TestClient:
    """Synchronous test client. Async test client requires extra setup; this
    is enough for endpoint smoke tests."""
    return TestClient(app)


def pytest_collection_modifyitems(config, items):
    if INTEGRATION_DATABASE_URL:
        return
    skip = pytest.mark.skip(reason="integration: set INTEGRATION_DATABASE_URL to a migrated Postgres (see .github/workflows/ci.yml)")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)
