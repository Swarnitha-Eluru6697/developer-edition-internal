"""Shared helpers for the alert integration tests (real Postgres): a clean database, a scripted world for the sweep to read, a webhook
receiver, and an in-memory secrets backend."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.services.alerts import sources
from app.services.alerts.engine import Obs

URL = os.environ.get("INTEGRATION_DATABASE_URL")
TABLES = "alert_events, alerts, alert_observations, alert_rules, alert_channels, alert_state"
SLACK = "https://hooks.slack.com/services/T000/B000/abcdWXYZ"
TEAMS = "https://contoso.webhook.office.com/webhookb2/aaaa/IncomingWebhook/bbbb/cccc"


async def clean() -> None:
    engine = create_async_engine(URL, poolclass=NullPool)
    async with engine.begin() as c:
        await c.execute(text(f"TRUNCATE {TABLES} CASCADE"))
    await engine.dispose()


def clean_sync() -> None:
    import asyncio
    asyncio.run(clean())


@pytest.fixture
async def db():
    await clean()
    engine = create_async_engine(URL, poolclass=NullPool)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


class FakeSecrets:
    def __init__(self):
        self.data: dict[str, dict] = {}

    async def get(self, path):
        return self.data.get(path)

    async def put(self, path, data):
        self.data[path] = data

    async def delete(self, path):
        self.data.pop(path, None)


@pytest.fixture
def secrets(monkeypatch):
    fake = FakeSecrets()
    from app.services.alerts import service
    monkeypatch.setattr(service, "get_secrets_backend", lambda: fake)
    return fake


class Receiver:
    """Plays Slack / Teams: records every post, answers with whatever `status` is set to."""

    def __init__(self):
        self.posts: list[tuple[str, dict]] = []
        self.status = 200
        self.text = "ok"
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self._handle))

    def _handle(self, req: httpx.Request) -> httpx.Response:
        import json
        self.posts.append((str(req.url), json.loads(req.content)))
        return httpx.Response(self.status, text=self.text)


@pytest.fixture
async def receiver(monkeypatch):
    from app.services.alerts import webhooks

    async def public(host):
        return None
    monkeypatch.setattr(webhooks, "assert_host_allowed", public)
    r = Receiver()
    yield r
    await r.client.aclose()


class World:
    """What the runners / Flink / Dagster would report on the next sweep (set the lists, then sweep)."""

    def __init__(self, now: datetime):
        self.now = now
        self.runs: list[Obs] = []
        self.flink: list[Obs] = []
        self.dagster: list[Obs] = []
        self.down: set[str] = set()

    def run(self, outcome, minutes_ago=1, *, job="j1", name="customers", kind="dbt", ref=None, detail=None, failure_class=None):
        ref = ref or f"run-{job}-{len(self.runs)}"
        self.runs.append(Obs(job, name, kind, "run", outcome, self.now - timedelta(minutes=minutes_ago), ref, failure_class, detail))
        return ref


@pytest.fixture
def world(monkeypatch):
    w = World(datetime.now(timezone.utc))

    async def runner(known, **kw):
        if "dbt-runner" in w.down:
            return sources.SourceResult("dbt-runner", False, "the dbt Runner could not be reached (ConnectError)"), {}
        return sources.SourceResult("dbt-runner", True, "", list(w.runs)), {o.job_key: (o.job_name, o.job_kind) for o in w.runs}

    async def flink(**kw):
        if "flink" in w.down:
            return sources.SourceResult("flink", False, "the FlinkSQL runner could not be reached (ConnectError)")
        return sources.SourceResult("flink", True, "", list(w.flink))

    async def dagster(known, names, **kw):
        return sources.SourceResult("dagster", True, "", list(w.dagster))
    monkeypatch.setattr(sources, "collect_runner", runner)
    monkeypatch.setattr(sources, "collect_flink", flink)
    monkeypatch.setattr(sources, "collect_dagster", dagster)
    return w
