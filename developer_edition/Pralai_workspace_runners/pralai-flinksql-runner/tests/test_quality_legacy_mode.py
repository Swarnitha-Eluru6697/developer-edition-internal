"""FL-007: legacy UCS delivery mode drops lifecycle/gate events for UCS, so it is reported as DEGRADED, and nothing it sets aside is lost:
the events are retained and re-queued for delivery the moment v2 delivery is enabled."""
from __future__ import annotations

from sqlalchemy import select

from app.core.config import Settings
from app.models import QualityResultOutbox
from app.services import quality_consumer as qc
from tests.quality_records import gate_event, window_result
from tests.test_quality_outbox_pg import RES, S, _entries, _settings, _Ucs


def test_delivery_mode_names_legacy_as_degraded():
    legacy = qc.delivery_mode(Settings(ucs_quality_runs_enabled=False))
    assert legacy["mode"] == "legacy" and legacy["degraded"] is True and "UCS_QUALITY_RUNS_ENABLED" in legacy["detail"]
    assert qc.delivery_mode(Settings(ucs_quality_runs_enabled=True)) == {"mode": "v2", "degraded": False}


async def test_legacy_mode_keeps_what_it_cannot_deliver_and_counts_it(pg_db):
    await qc._write_outbox(_entries(window_result()) + [qc.process_record(S.quality_gate_topic, 0, 9, gate_event(), S)])
    await qc._drain_outbox(_Ucs(), _settings(ucs_quality_runs_enabled=False))
    async with pg_db() as db:
        rows = list((await db.execute(select(QualityResultOutbox))).scalars())
    skipped = [r for r in rows if r.status == "skipped"]
    assert {r.payload.get("contract") for r in skipped} >= {"inaiera.dq.gate-event"} and len(skipped) == 2     # 'started' + the gate event
    assert all(r.last_error == qc.LEGACY_SKIP_REASON for r in skipped)
    assert await qc.legacy_backlog_count() == 2


async def test_enabling_v2_requeues_and_delivers_what_legacy_set_aside(pg_db):
    await qc._write_outbox(_entries(window_result()) + [qc.process_record(S.quality_gate_topic, 0, 9, gate_event(), S)])
    await qc._drain_outbox(_Ucs(), _settings(ucs_quality_runs_enabled=False))
    qc.reset_state()                                                              # a process restart with v2 turned on
    ucs = _Ucs()
    await qc._drain_outbox(ucs, _settings(ucs_quality_runs_enabled=True))
    paths = sorted(u.rsplit("/", 1)[-1] for u, _, _ in ucs.posts)
    assert paths == ["gate-events", "runs"]                                       # the 'started' run and the gate event, now delivered
    async with pg_db() as db:
        statuses = {r.status for r in (await db.execute(select(QualityResultOutbox))).scalars()}
    assert "skipped" not in statuses
    assert await qc.legacy_backlog_count() == 0


async def test_requeue_does_not_touch_tombstones_or_other_skips(pg_db):
    await qc._write_outbox([qc.tombstone_entry(RES, 0, 1)])
    assert await qc.requeue_legacy_skipped() == 0
    async with pg_db() as db:
        (row,) = (await db.execute(select(QualityResultOutbox))).scalars().all()
    assert row.status == "skipped" and row.kind == "dead_letter"
