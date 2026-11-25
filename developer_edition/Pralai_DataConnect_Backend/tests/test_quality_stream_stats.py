"""
QualityStreamStats — the live, continuously-updated running totals for
QualityStreamWorker's real-time enforcement (see app/models/quality.py's
QualityStreamStats docstring for why this is deliberately separate from
QualityResult, the discrete Execute-button/execute-job history).

Consistent with the rest of this suite: everything here is tested against a
mocked repository/session boundary, never a real Postgres connection (see
app/repositories/dataconnect.py's QualityStreamStatsRepository.record_outcome
for the actual upsert SQL, which is standard SQLAlchemy
insert().on_conflict_do_update() — not something worth re-deriving in a unit
test with a fake session).
"""

import datetime

import pytest
from conftest import DEFAULT_GATEWAY_HEADERS
from fastapi.testclient import TestClient

from app.main import app
from app.services.quality_stream_worker import QualityStreamWorker

client = TestClient(app, headers=DEFAULT_GATEWAY_HEADERS)

PIPELINE = {
    "id": "pipe_1",
    "org_id": "org_1001",
    "topic_name": "raw__db__postgres__products__batch__v1",
    "domain": "db",
    "source_system": "postgres",
    "entity": "products",
    "version": "v1",
    "schema_version_id": None,
    "connector_config": {},
    "quality_policy_id": "dqp_1",
}
POLICY = {
    "id": "dqp_1",
    "rules": [{"name": "stock_range", "type": "min_max_numeric", "field": "stock_quantity", "min": 20, "max": 150}],
}


def _worker() -> QualityStreamWorker:
    return QualityStreamWorker(org_id="org_1001", pipeline=PIPELINE, policy=POLICY)


# ── QualityStreamWorker._record_outcome ──────────────────────────────────────


@pytest.mark.asyncio
async def test_record_outcome_calls_the_repository_with_the_right_args(monkeypatch) -> None:
    calls = []
    sample_calls = []

    async def _fake_record_outcome(self, **kwargs):
        calls.append(kwargs)

    async def _fake_append_sample_failure(self, **kwargs):
        sample_calls.append(kwargs)

    monkeypatch.setattr(
        "app.services.quality_stream_worker.QualityStreamStatsRepository.record_outcome", _fake_record_outcome
    )
    monkeypatch.setattr(
        "app.services.quality_stream_worker.QualityStreamStatsRepository.append_sample_failure",
        _fake_append_sample_failure,
    )
    failure_count_calls = []

    async def _fake_record_validation_failure(session, **kwargs):
        failure_count_calls.append(kwargs)

    monkeypatch.setattr(
        "app.services.quality_stream_worker.record_validation_failure", _fake_record_validation_failure
    )

    await _worker()._record_outcome(valid=True, failed_rules=[], original_record={"stock_quantity": 50})
    await _worker()._record_outcome(
        valid=False, failed_rules=["stock_range"], original_record={"stock_quantity": 5},
    )

    assert calls == [
        {"org_id": "org_1001", "pipeline_id": "pipe_1", "quality_policy_id": "dqp_1", "valid": True},
        {"org_id": "org_1001", "pipeline_id": "pipe_1", "quality_policy_id": "dqp_1", "valid": False},
    ]
    # A passing record never gets an envelope; a failing one does, carrying
    # the failed rule name and original record.
    assert len(sample_calls) == 1
    envelope = sample_calls[0]["envelope"]
    assert sample_calls[0]["org_id"] == "org_1001"
    assert sample_calls[0]["pipeline_id"] == "pipe_1"
    assert envelope["failed_rules"] == ["stock_range"]
    assert envelope["original_record"] == {"stock_quantity": 5}
    assert envelope["pipeline_id"] == "pipe_1"
    assert envelope["quality_policy_id"] == "dqp_1"
    # FE-BUG-17: the exact per-rule counter is incremented for the failing record only.
    assert failure_count_calls == [
        {"org_id": "org_1001", "pipeline_id": "pipe_1", "failed_rules": ["stock_range"]},
    ]


@pytest.mark.asyncio
async def test_record_outcome_propagates_a_persistence_failure(monkeypatch) -> None:
    """A stats-write failure must be visible to the caller (_with_retry
    in _handle) so a transient DB hiccup gets retried rather than silently
    losing that record's quality observation forever — see BUG-03. It's
    still logged here under its own distinct event name first (so a stats
    failure is identifiable as that, not a bare "record failed"), but it
    must re-raise, not swallow."""

    async def _boom(self, **kwargs):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(
        "app.services.quality_stream_worker.QualityStreamStatsRepository.record_outcome", _boom
    )

    with pytest.raises(RuntimeError, match="db unavailable"):
        await _worker()._record_outcome(valid=True, failed_rules=[], original_record={})


@pytest.mark.asyncio
async def test_handle_records_an_outcome_for_both_passing_and_failing_records(monkeypatch) -> None:
    """_handle is now a pure observer — no producer, no key, no Kafka
    output at all — see quality_stream_worker.py's own docstring on why the
    intake/pass/quarantine routing this test used to exercise was removed."""
    seen = []

    async def _fake_record_outcome(self, *, valid: bool, **_kwargs) -> None:
        seen.append(valid)

    async def _no_quarantine(self, **_kwargs) -> None:
        pass  # this test is about the stats outcome; the quarantine copy has its own tests

    monkeypatch.setattr(QualityStreamWorker, "_record_outcome", _fake_record_outcome)
    monkeypatch.setattr(QualityStreamWorker, "_quarantine", _no_quarantine)
    worker = _worker()

    await worker._handle(envelope={"stock_quantity": 50})  # passes
    await worker._handle(envelope={"stock_quantity": 5})  # fails

    assert seen == [True, False]


# ── QualityStreamStatsRepository.append_sample_failure ───────────────────────


@pytest.mark.asyncio
async def test_append_sample_failure_appends_and_caps_at_the_configured_limit(monkeypatch) -> None:
    from app.repositories.dataconnect import QualityStreamStatsRepository

    monkeypatch.setattr("app.repositories.dataconnect.settings.quality_stream_max_sample_failures", 3)
    row = _FakeRow(pipeline_id="pipe_1", sample_failures=[{"n": 1}, {"n": 2}, {"n": 3}])
    repo = QualityStreamStatsRepository(session=None)
    monkeypatch.setattr(repo, "get_by_pipeline_id", lambda org_id, pipeline_id: _async_return(row))

    await repo.append_sample_failure(org_id="org_1001", pipeline_id="pipe_1", envelope={"n": 4})

    # Oldest ({"n": 1}) is dropped to keep exactly the cap's worth, newest last.
    assert row.sample_failures == [{"n": 2}, {"n": 3}, {"n": 4}]


@pytest.mark.asyncio
async def test_append_sample_failure_is_a_noop_when_no_row_exists_yet(monkeypatch) -> None:
    from app.repositories.dataconnect import QualityStreamStatsRepository

    repo = QualityStreamStatsRepository(session=None)
    monkeypatch.setattr(repo, "get_by_pipeline_id", lambda org_id, pipeline_id: _async_return(None))

    await repo.append_sample_failure(org_id="org_1001", pipeline_id="pipe_1", envelope={"n": 1})  # must not raise


async def _async_return(value):
    return value


# ── QualityService.list_stream_stats ─────────────────────────────────────────


class _FakeRow:
    def __init__(self, **kw):
        self.__dict__.update(kw)


@pytest.mark.asyncio
async def test_list_stream_stats_derives_passed_count(monkeypatch) -> None:
    from app.services.quality_service import quality_service

    row = _FakeRow(
        id="qss_1", org_id="org_1001", pipeline_id="pipe_1", quality_policy_id="dqp_1",
        total_count=57, failed_count=14, sample_failures=[{"failed_rules": ["stock_range"]}],
        updated_at=datetime.datetime(2026, 1, 1),
    )

    async def _list_for_org(self, org_id):
        return [row]

    monkeypatch.setattr(
        "app.repositories.dataconnect.QualityStreamStatsRepository.list_for_org", _list_for_org
    )

    results = await quality_service.list_stream_stats("org_1001")

    assert results == [{
        "id": "qss_1", "org_id": "org_1001", "pipeline_id": "pipe_1", "quality_policy_id": "dqp_1",
        "total_count": 57, "failed_count": 14, "passed_count": 43,
        "sample_failures": [{"failed_rules": ["stock_range"]}],
        "updated_at": datetime.datetime(2026, 1, 1),
    }]


# ── GET /quality-stream-stats ─────────────────────────────────────────────────


def test_quality_stream_stats_route_returns_the_service_list(monkeypatch) -> None:
    async def _list_stream_stats(org_id):
        return [{
            "id": "qss_1", "org_id": "org_1001", "pipeline_id": "pipe_1", "quality_policy_id": "dqp_1",
            "total_count": 57, "failed_count": 14, "passed_count": 43,
            "updated_at": "2026-01-01T00:00:00Z",
        }]

    monkeypatch.setattr("app.api.routes.quality.quality_service.list_stream_stats", _list_stream_stats)

    resp = client.get("/api/v1/dataconnect/quality-stream-stats", headers={"X-Tenant-Id": "org_1001"})

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["pipeline_id"] == "pipe_1"
    assert body[0]["total_count"] == 57
    assert body[0]["failed_count"] == 14
    assert body[0]["passed_count"] == 43
