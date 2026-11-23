from conftest import DEFAULT_GATEWAY_HEADERS
from fastapi.testclient import TestClient

import app.api.routes.health as health_routes
from app.core.config import settings
from app.main import app

client = TestClient(app, headers=DEFAULT_GATEWAY_HEADERS)
_REAL_CHECK_SECRET_STORE = health_routes._check_secret_store   # before the autouse stub below replaces it


async def _never_call(*args, **kwargs):
    raise AssertionError("liveness must never call a dependency check")


def test_health_endpoint_ok() -> None:
    resp = client.get("/api/v1/dataconnect/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "service": "dataconnect"}


def test_liveness_endpoint_ok() -> None:
    resp = client.get("/api/v1/dataconnect/health/live")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "service": "dataconnect"}


def test_liveness_never_touches_postgres_or_kafka_connect(monkeypatch) -> None:
    """The core of the split: liveness must have zero dependencies — proven
    here by making both checks blow up if they're ever called."""
    monkeypatch.setattr(health_routes, "_check_postgres", _never_call)
    monkeypatch.setattr(health_routes, "_check_kafka_connect", _never_call)

    resp = client.get("/api/v1/dataconnect/health/live")

    assert resp.status_code == 200


def test_health_alias_also_never_touches_dependencies(monkeypatch) -> None:
    monkeypatch.setattr(health_routes, "_check_postgres", _never_call)
    monkeypatch.setattr(health_routes, "_check_kafka_connect", _never_call)

    resp = client.get("/api/v1/dataconnect/health")

    assert resp.status_code == 200


async def _ok_check():
    return {"ok": True}


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _secret_store_ready_by_default(monkeypatch):
    """Readiness now also asks the secret store; these tests are about the other checks, so it answers ready unless a test says otherwise."""
    monkeypatch.setattr(health_routes, "_check_secret_store", _ok_check)


async def _failing_check():
    return {"ok": False, "error": "connection refused"}


def test_readiness_ok_when_postgres_reachable_and_runtime_deploy_disabled(monkeypatch) -> None:
    monkeypatch.setattr(settings, "runtime_deploy_enabled", False)
    monkeypatch.setattr(health_routes, "_check_postgres", _ok_check)
    monkeypatch.setattr(health_routes, "_check_kafka_connect", _never_call)  # must be skipped, not called

    resp = client.get("/api/v1/dataconnect/health/ready")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert body["checks"]["postgres"]["ok"] is True
    assert body["checks"]["kafka_connect"]["ok"] is True
    assert "skipped" in body["checks"]["kafka_connect"]


def test_readiness_returns_503_when_postgres_unreachable(monkeypatch) -> None:
    monkeypatch.setattr(settings, "runtime_deploy_enabled", False)
    monkeypatch.setattr(health_routes, "_check_postgres", _failing_check)

    resp = client.get("/api/v1/dataconnect/health/ready")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["postgres"]["ok"] is False


def test_readiness_checks_kafka_connect_when_runtime_deploy_enabled(monkeypatch) -> None:
    monkeypatch.setattr(settings, "runtime_deploy_enabled", True)
    monkeypatch.setattr(health_routes, "_check_postgres", _ok_check)
    monkeypatch.setattr(health_routes, "_check_kafka_connect", _ok_check)

    resp = client.get("/api/v1/dataconnect/health/ready")

    assert resp.status_code == 200
    assert resp.json()["checks"]["kafka_connect"] == {"ok": True}


def test_readiness_returns_503_when_kafka_connect_unreachable_and_deploy_enabled(monkeypatch) -> None:
    monkeypatch.setattr(settings, "runtime_deploy_enabled", True)
    monkeypatch.setattr(health_routes, "_check_postgres", _ok_check)
    monkeypatch.setattr(health_routes, "_check_kafka_connect", _failing_check)

    resp = client.get("/api/v1/dataconnect/health/ready")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["kafka_connect"]["ok"] is False
    assert body["checks"]["postgres"]["ok"] is True


# ── probes carry no identity ───────────────────────────────────────────────────────────────────────────────────────

anonymous = TestClient(app)  # what a container health check, a load balancer or an orchestrator sends: no headers at all


def test_the_container_health_checks_reach_liveness_without_any_identity() -> None:
    """The compose/Docker health check (and any orchestrator probe) sends no identity at all. Behind Gate 1 it would be
    answered 401, so the backend would be permanently 'unhealthy' while serving requests perfectly well — and a
    Kubernetes liveness probe would restart it forever. The health paths are the gateway's public paths."""
    for path in ("/api/v1/dataconnect/health", "/api/v1/dataconnect/health/live"):
        resp = anonymous.get(path)

        assert resp.status_code == 200, path
        assert resp.json() == {"status": "ok", "service": "dataconnect"}


def test_readiness_is_probeable_without_identity_but_reveals_no_infrastructure_detail(monkeypatch) -> None:
    monkeypatch.setattr(settings, "runtime_deploy_enabled", True)

    async def _postgres_down():
        return {"ok": False, "error": "connection to server at \"10.0.0.5\", port 5432 failed: password=hunter2"}

    async def _connect_down():
        return {
            "ok": False, "error": "refused http://connect-cdc:8083",
            "clusters": {"http://connect-cdc:8083": {"ok": False, "pools": ["connect-cdc-pool"], "error": "refused"},
                         "http://shared:8083": {"ok": True, "pools": ["connect-jdbc-pool"]}},
        }

    monkeypatch.setattr(health_routes, "_check_postgres", _postgres_down)
    monkeypatch.setattr(health_routes, "_check_kafka_connect", _connect_down)

    resp = anonymous.get("/api/v1/dataconnect/health/ready")

    assert resp.status_code == 503
    body = resp.json()
    assert body["checks"]["postgres"] == {"ok": False}
    assert body["checks"]["kafka_connect"] == {"ok": False, "unreachable_pools": ["connect-cdc-pool"]}
    leaked = resp.text
    for sensitive in ("10.0.0.5", "hunter2", "connect-cdc:8083", "shared:8083", "refused"):
        assert sensitive not in leaked


def test_every_other_route_still_needs_an_identity() -> None:
    assert anonymous.get("/api/v1/dataconnect/pipelines").status_code in (401, 403)
    assert anonymous.get("/api/v1/dataconnect/connections").status_code in (401, 403)


# ── GAP-05: readiness asks the secret store it actually uses ────────────────────────────────────────────────────────

LEAKY = "postgresql://svc:hunter2@db.internal:5432/x refused the connection"


def test_readiness_reports_the_secret_store_backend_when_ready(monkeypatch) -> None:
    async def _ready():
        return {"ok": True, "backend": "postgres"}

    monkeypatch.setattr(settings, "runtime_deploy_enabled", False)
    monkeypatch.setattr(health_routes, "_check_postgres", _ok_check)
    monkeypatch.setattr(health_routes, "_check_secret_store", _ready)

    resp = client.get("/api/v1/dataconnect/health/ready")

    assert resp.status_code == 200
    assert resp.json()["checks"]["secret_store"] == {"ok": True, "backend": "postgres"}


def test_readiness_is_503_when_the_secret_store_cannot_store_secrets_and_leaks_nothing(monkeypatch) -> None:
    async def _broken():
        return {"ok": False, "backend": "postgres", "error": LEAKY}

    monkeypatch.setattr(settings, "runtime_deploy_enabled", False)
    monkeypatch.setattr(health_routes, "_check_postgres", _ok_check)
    monkeypatch.setattr(health_routes, "_check_secret_store", _broken)

    resp = client.get("/api/v1/dataconnect/health/ready")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["secret_store"] == {"ok": False, "backend": "postgres"}
    assert "hunter2" not in resp.text
    assert "error" not in body["checks"]["secret_store"]


def test_the_secret_store_check_runs_the_real_store_and_is_ok_with_a_working_key(monkeypatch) -> None:
    import asyncio

    from app.services.secret_store import PostgresSecretStore

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, model, key):
            return None                                             # the probe row does not exist: a readable, empty answer

    monkeypatch.setattr("app.services.secret_store.AsyncSessionLocal", lambda: _Session())

    assert asyncio.run(PostgresSecretStore().check_ready()) == {"ok": True, "backend": "postgres"}


def test_the_secret_store_is_not_ready_without_an_encryption_key_and_says_only_the_class_name(monkeypatch) -> None:
    import asyncio

    from app.services.secret_store import PostgresSecretStore

    monkeypatch.setattr(settings, "secret_encryption_key", "")

    result = asyncio.run(PostgresSecretStore().check_ready())

    assert result == {"ok": False, "backend": "postgres", "error": "SecretEncryptionError"}


def test_the_secret_store_is_not_ready_when_its_table_cannot_be_read_and_leaks_no_message(monkeypatch) -> None:
    import asyncio

    from app.services.secret_store import PostgresSecretStore

    class _Down:
        async def __aenter__(self):
            raise ConnectionError(LEAKY)

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr("app.services.secret_store.AsyncSessionLocal", lambda: _Down())

    result = asyncio.run(PostgresSecretStore().check_ready())

    assert result == {"ok": False, "backend": "postgres", "error": "ConnectionError"}
    assert "hunter2" not in str(result)


def test_readiness_wiring_calls_the_store_in_use(monkeypatch) -> None:
    """`_check_secret_store` asks `app.services.secret_store.secret_store` — whatever store is configured — not a fixed one."""
    import asyncio

    import app.services.secret_store as secret_store_module

    class _Other:
        async def check_ready(self):
            return {"ok": True, "backend": "other"}

    monkeypatch.setattr(secret_store_module, "secret_store", _Other())

    assert asyncio.run(_REAL_CHECK_SECRET_STORE()) == {"ok": True, "backend": "other"}
