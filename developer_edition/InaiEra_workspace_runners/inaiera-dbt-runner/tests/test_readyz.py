"""/readyz must return 503 when an enabled dependency is unusable — not
just when the DB is down — and report outbox trouble as "degraded"."""
from __future__ import annotations

import uuid

import httpx
import pytest

from app import main
from app.core.auth import _jwks_cache
from app.core.config import get_settings
from app.main import app
from app.models import MetadataOutboxEvent, Project, Run


@pytest.fixture
async def api(run_db, monkeypatch):
    main._readiness_cache.update({"jwks_retry_at": 0.0, "k8s_checked_at": 0.0, "k8s_ok": None})
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_ready_when_db_and_jwks_ok(api):
    r = await api.get("/readyz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


async def test_503_when_jwks_not_primed(api, monkeypatch):
    _jwks_cache._keys = {}

    async def failing_refresh():
        return None
    monkeypatch.setattr(_jwks_cache, "refresh", failing_refresh)
    r = await api.get("/readyz")
    assert r.status_code == 503
    assert r.json()["checks"]["jwks"] == "not_primed"


async def test_503_when_k8s_api_unreachable_in_k8s_job_mode(api, monkeypatch):
    monkeypatch.setattr(get_settings(), "runner_mode", "k8s_job")

    class Iso:
        async def k8s_reachable(self):
            return False
    monkeypatch.setattr("app.executors.isolation.get_runner_isolation", lambda: Iso())
    r = await api.get("/readyz")
    assert r.status_code == 503
    assert r.json()["checks"]["k8s_api"] == "unreachable"


async def test_503_when_db_down(monkeypatch):
    class Boom:
        async def __aenter__(self):
            raise RuntimeError("db down")

        async def __aexit__(self, *a):
            return False
    monkeypatch.setattr("app.db.session.AsyncSessionLocal", lambda: Boom())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/readyz")
    assert r.status_code == 503


async def test_dead_letters_report_degraded_but_ready(api, run_db):
    async with run_db() as db:
        p = Project(org_id="o", name="n", git_url="https://g/x.git", git_ref="main", engine="pg_lake")
        db.add(p)
        await db.flush()
        run = Run(project_id=p.id, trigger="manual", status="succeeded")
        db.add(run)
        await db.flush()
        db.add(MetadataOutboxEvent(run_id=run.id, project_id=p.id, event_type="openlineage",
                                   idempotency_key=f"openlineage:{uuid.uuid4()}", payload={},
                                   status="dead_letter", attempt_count=20))
        await db.commit()
    r = await api.get("/readyz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "degraded"
    assert body["checks"]["outbox"]["dead_letter"] == 1
