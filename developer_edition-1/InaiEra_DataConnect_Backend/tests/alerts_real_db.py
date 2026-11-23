"""Shared set-up for the alerts tests that need a REAL PostgreSQL (DATACONNECT_REAL_DB_TESTS=1; see tests/scratch_db.py).

One scratch database per test module, built the way a fresh install is (app.startup.run_migrations: schema from the models, stamped at head;
migration 0055 itself is exercised by tests/test_migrations_real_db.py), dropped afterwards. Every test gets its own organization, so tests in one module never see each other's rows. Webhook posts go to a recording
fake (`Hooks`), never the network; the SSRF guard's DNS lookup is replaced too.
"""

import json
import os
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

REAL_DB = bool(os.environ.get("DATACONNECT_REAL_DB_TESTS"))
skip_without_db = pytest.mark.skipif(not REAL_DB, reason="needs a real PostgreSQL (DATACONNECT_REAL_DB_TESTS=1)")

SLACK = "https://hooks.slack.com/services/T000/B000/XXXXsecretXXXX"
TEAMS = "https://prod-12.westus.logic.azure.com/workflows/abc/triggers/manual/paths/invoke?sig=SECRETSIG"


def now() -> datetime:
    return datetime.now(UTC)


@pytest.fixture(scope="module", name="scratch_url")
def scratch_url_fixture():
    from scratch_db import scratch_database

    with scratch_database("dc_alerts") as scratch:
        result = scratch.startup_migrations()
        assert result.returncode == 0, result.stderr[-2000:]
        scratch.execute("""
            INSERT INTO dc_connector_templates (id, name, connector_type, category, connector_class, runtime_pool_type, config_schema, default_config,
                default_transforms, version, is_active, supported_transforms, supported_kafka_connect_versions, supported_source_versions,
                transform_compatibility, supported_modes, supports_dlq, supports_schema_discovery)
              VALUES ('tpl_src', 'T', 'source', 'database', 'x.Y', 'connect-jdbc-pool', '{}', '{}', '[]', '1', true, '[]', '[]', '[]', '{}', '[]', false, true);
        """)
        yield scratch.url


@pytest.fixture(name="factory")
async def factory_fixture(scratch_url, monkeypatch):
    """A session factory on the scratch database, also used by record_audit and the sweeper (they open their own sessions)."""
    engine = create_async_engine(scratch_url, poolclass=NullPool)
    make = async_sessionmaker(engine, expire_on_commit=False)
    import app.core.audit as audit
    import app.services.alerts.sweeper as sweeper
    monkeypatch.setattr(audit, "AsyncSessionLocal", make)
    monkeypatch.setattr(sweeper, "AsyncSessionLocal", make)

    from app.services.alerts import webhooks

    async def public(host, *, org_id=None, purpose=""):          # no DNS in tests: provider hosts count as public
        return None
    monkeypatch.setattr(webhooks, "validate_destination", public)
    yield make
    await engine.dispose()


class Hooks:
    """A fake Slack / Teams: records every post; `fail` makes it answer 500 for the URLs given."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict]] = []
        self.fail: set[str] = set()

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.posts.append((url, json.loads(request.content)))
        return httpx.Response(500, text="boom") if url in self.fail else httpx.Response(200, text="ok")

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))

    def texts(self, url: str | None = None) -> list[str]:
        """Every post's text, as one flat string each (whatever the provider's format)."""
        return [json.dumps(body) for u, body in self.posts if url is None or u == url]


@pytest.fixture(name="hooks")
def hooks_fixture(monkeypatch):
    """Every webhook post anywhere in the app goes to the fake (the API's Send test uses no injected client)."""
    h = Hooks()
    real = httpx.AsyncClient

    def patched(*a, **kw):
        kw.setdefault("transport", httpx.MockTransport(h.handler))
        return real(*a, **kw)
    from app.services.alerts import webhooks
    monkeypatch.setattr(webhooks.httpx, "AsyncClient", patched)
    return h


async def new_org(make) -> str:
    org = f"org_{uuid.uuid4().hex[:10]}"
    async with make() as s:
        await s.execute(text("INSERT INTO dc_organizations (org_id, name, status) VALUES (:o, :o, 'active')"), {"o": org})
        await s.execute(text("INSERT INTO dc_connection_profiles (id, org_id, name, source_type, connection_config) "
                             "VALUES (:id, :o, 'conn', 'postgresql', '{}')"), {"id": str(uuid.uuid4()), "o": org})
        await s.commit()
    return org


async def add_pipeline(make, org: str, name: str, *, direction: str = "SOURCE", status: str = "running") -> str:
    pid = str(uuid.uuid4())
    async with make() as s:
        conn = (await s.execute(text("SELECT id FROM dc_connection_profiles WHERE org_id = :o LIMIT 1"), {"o": org})).scalar()
        await s.execute(text("""
            INSERT INTO dc_pipelines (id, org_id, name, template_id, connection_profile_id, zone, domain, source_system, entity, mode, version,
                direction, connector_name, topic_name, connector_config, generated_config, template_snapshot, status, error_handling_policy,
                runtime_pool_id)
              VALUES (:id, :o, :n, 'tpl_src', :c, 'raw', 'd', 's', :n, 'batch', 'v1', :dir, :cn, :topic, :cc, '{}', '{}', :st, 'SKIP_AND_LOG',
                      'connect-jdbc-pool')"""),
            {"id": pid, "o": org, "n": name, "c": conn, "dir": direction, "cn": f"d--s--{name}--batch--v1", "st": status,
             # a source produces its own topic; a sink produces none and reads one (the dc_pipelines check constraint)
             "topic": f"raw-d-s-{name}-batch-v1" if direction == "SOURCE" else None,
             "cc": "{}" if direction == "SOURCE" else '{"topics": "raw-d-s-in-batch-v1"}'})
        await s.commit()
    return pid


async def set_state(make, org: str, pid: str, state: str, *, error: str | None = None, observed_at: datetime | None = None) -> None:
    """What the reconciler would write after asking Kafka Connect."""
    async with make() as s:
        await s.execute(text("""
            INSERT INTO dc_pipeline_runtime_state (id, org_id, pipeline_id, connector_name, desired_state, observed_state, last_runtime_error,
                last_observed_at, desired_generation, observed_generation)
              VALUES (:id, :o, :p, 'c', 'running', :st, :err, :at, 1, 1)
            ON CONFLICT (pipeline_id) DO UPDATE SET observed_state = :st, last_runtime_error = :err, last_observed_at = :at"""),
            {"id": str(uuid.uuid4()), "o": org, "p": pid, "st": state, "err": error, "at": observed_at or now()})
        await s.commit()


async def add_operation(make, org: str, pid: str, status: str, *, kind: str = "pipeline_deploy", minutes_ago: float = 1,
                        message: str | None = None) -> str:
    oid = str(uuid.uuid4())
    async with make() as s:
        await s.execute(text("""
            INSERT INTO dc_operations (id, org_id, operation_type, resource_type, resource_id, status, error_message, error_code, completed_at,
                attempt_count)
              VALUES (:id, :o, :k, 'pipeline', :p, :st, :m, :code, :at, 1)"""),
            {"id": oid, "o": org, "k": kind, "p": pid, "st": status, "m": message, "code": "KAFKA_CONNECT_CONFIG_INVALID" if message else None,
             "at": now() - timedelta(minutes=minutes_ago)})
        await s.commit()
    return oid


async def add_threshold_pause(make, org: str, pid: str, *, minutes_ago: float = 1) -> None:
    async with make() as s:
        await s.execute(text("""
            INSERT INTO dc_audit_logs (id, org_id, actor, action, resource_type, resource_id, after_state, created_at)
              VALUES (:id, :o, 'system:error-threshold', 'paused_threshold_exceeded', 'pipeline', :p,
                      '{"error_count": 250, "max_errors": 100, "window_seconds": 300}', :at)"""),
            {"id": str(uuid.uuid4()), "o": org, "p": pid, "at": now() - timedelta(minutes=minutes_ago)})
        await s.commit()


async def add_failures(make, org: str, pid: str, kind: str, count: int, *, minutes_ago: float = 1, key: str | None = None) -> None:
    """kind: ingestion (dead-letter) or validation (key "*" = failing records)."""
    bucket = (now() - timedelta(minutes=minutes_ago)).replace(second=0, microsecond=0)
    async with make() as s:
        await s.execute(text("""
            INSERT INTO dc_failure_counts (id, org_id, pipeline_id, kind, key, bucket_start, count)
              VALUES (:id, :o, :p, :k, :key, :b, :n)"""),
            {"id": str(uuid.uuid4()), "o": org, "p": pid, "k": kind, "key": key or ("*" if kind == "validation" else "connector"),
             "b": bucket, "n": count})
        await s.commit()


async def delete_pipeline(make, pid: str) -> None:
    async with make() as s:
        await s.execute(text("DELETE FROM dc_pipeline_runtime_state WHERE pipeline_id = :p"), {"p": pid})
        await s.execute(text("DELETE FROM dc_pipelines WHERE id = :p"), {"p": pid})
        await s.commit()
