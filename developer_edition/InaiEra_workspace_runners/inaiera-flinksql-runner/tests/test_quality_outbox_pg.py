"""Outbox + lifecycle against real Postgres (throwaway test DB):
SKIP LOCKED drains, semantic idempotency, started/stale lifecycle,
dead letters, v2 vs legacy delivery, gate fail-stop."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from sqlalchemy import select, text, update

from app.core.config import Settings, get_settings
from app.models import QualityCheckHeartbeat, QualityResultOutbox
from app.services import quality_consumer as qc
from tests.quality_records import gate_event, window_result

S = get_settings()
RES, GATE = S.quality_results_topic, S.quality_gate_topic


def _settings(**kw):
    base = dict(ucs_url="http://ucs", ucs_internal_token="k", quality_consumer_batch_size=50,
                quality_consumer_retry_backoff_seconds=0.01, org_id=S.org_id)
    base.update(kw)
    return Settings(**base)


class _Ucs:
    def __init__(self, status=201, delay=0.0):
        self.posts: list[tuple[str, str, dict]] = []
        self.status, self.delay = status, delay

    async def post(self, url, json=None, headers=None, timeout=None):
        self.posts.append((url, headers["Idempotency-Key"], json))
        await asyncio.sleep(self.delay)
        status = self.status

        class _R:
            status_code = status
            text = ""
        return _R()


async def _rows(pg_db, model=QualityResultOutbox):
    async with pg_db() as db:
        return list((await db.execute(select(model))).scalars())


def _entries(*records, topic=RES):
    return [qc.process_record(topic, 0, i, r, S) for i, r in enumerate(records)]


async def test_first_window_emits_started_then_completed_and_replays_collapse(pg_db):
    await qc._write_outbox(_entries(window_result()))
    await qc._write_outbox(_entries(window_result()))                 # Flink replay
    await qc._write_outbox(_entries(window_result(window_start="2026-10-01T00:01:00",
                                                  window_end="2026-10-01T00:02:00")))
    rows = await _rows(pg_db)
    statuses = sorted(r.payload["status"] for r in rows)
    assert statuses == ["completed_fail", "completed_fail", "started"]
    (hb,) = await _rows(pg_db, QualityCheckHeartbeat)
    assert hb.state == "active" and hb.last_window_end == "2026-10-01T00:02:00"


async def test_concurrent_drains_post_each_row_exactly_once(pg_db):
    await qc._write_outbox(_entries(*[window_result(check_id=f"c{i}") for i in range(5)]))
    ucs = _Ucs(delay=0.05)
    s = _settings(ucs_quality_runs_enabled=True)
    results = await asyncio.gather(qc._drain_outbox(ucs, s), qc._drain_outbox(ucs, s))
    assert sum(sent for sent, _ in results) == 10            # 5 started + 5 completed
    keys = [k for _, k, _ in ucs.posts]
    assert len(keys) == len(set(keys)) == 10
    assert all(u.endswith("/api/v1/quality/runs") for u, _, _ in ucs.posts)
    assert {r.status for r in await _rows(pg_db)} == {"delivered"}


async def test_legacy_mode_delivers_completed_and_skips_lifecycle(pg_db):
    await qc._write_outbox(_entries(window_result()))
    ucs = _Ucs()
    await qc._drain_outbox(ucs, _settings())
    by_status = {r.payload["status"]: r for r in await _rows(pg_db)}
    assert by_status["completed_fail"].status == "delivered"
    assert by_status["started"].status == "skipped"
    (url, key, body), = ucs.posts
    assert url.endswith("/api/v1/quality/results") and body["status"] == "fail"
    assert key == by_status["completed_fail"].idempotency_key


async def test_ucs_rejection_is_not_retried_forever(pg_db):
    await qc._write_outbox(_entries(window_result()))
    await qc._drain_outbox(_Ucs(status=422), _settings(ucs_quality_runs_enabled=True))
    assert {r.status for r in await _rows(pg_db)} == {"rejected"}


async def test_ucs_outage_keeps_rows_pending(pg_db):
    await qc._write_outbox(_entries(window_result()))
    await qc._drain_outbox(_Ucs(status=503), _settings(ucs_quality_runs_enabled=True))
    rows = await _rows(pg_db)
    assert {r.status for r in rows} == {"pending"} and any(r.attempts == 1 for r in rows)


async def test_dead_letters_are_stored_never_delivered(pg_db):
    await qc._write_outbox(_entries({"rule_name": "old-shape"}))
    (row,) = await _rows(pg_db)
    assert row.kind == "dead_letter" and row.status == "rejected" and row.last_error
    ucs = _Ucs()
    await qc._drain_outbox(ucs, _settings(ucs_quality_runs_enabled=True))
    assert ucs.posts == []


async def test_stale_heartbeat_emits_one_stale_run(pg_db):
    await qc._write_outbox(_entries(window_result()))
    async with pg_db() as db:
        await db.execute(update(QualityCheckHeartbeat).values(
            last_seen_at=datetime.now(timezone.utc) - timedelta(hours=1)))
        await db.commit()
    s = _settings(quality_check_stale_after_seconds=60)
    assert await qc._sweep_stale(s) == 1
    assert await qc._sweep_stale(s) == 0
    stale = [r for r in await _rows(pg_db) if r.payload["status"] == "stale"]
    assert len(stale) == 1 and stale[0].payload["run_ref"] == "dep-1"
    (hb,) = await _rows(pg_db, QualityCheckHeartbeat)
    assert hb.state == "stale"
    await qc._write_outbox(_entries(window_result(window_end="2026-10-01T00:05:00")))
    (hb,) = await _rows(pg_db, QualityCheckHeartbeat)
    assert hb.state == "active"                                      # resumed


async def test_recent_heartbeat_is_not_stale(pg_db):
    await qc._write_outbox(_entries(window_result()))
    assert await qc._sweep_stale(_settings(quality_check_stale_after_seconds=60)) == 0


async def test_gate_fail_stop_stops_parent_job_once_when_opted_in(pg_db):
    await qc._write_outbox(_entries(gate_event(action="fail_stop"), topic=GATE))
    stop = AsyncMock(return_value=type("D", (), {"id": "dep-9"})())
    with patch("app.services.job_service.JobService.stop", stop):
        await qc._drain_outbox(_Ucs(), _settings(ucs_quality_runs_enabled=True))
        await qc._drain_outbox(_Ucs(), _settings(ucs_quality_runs_enabled=True))
    assert stop.await_count == 1
    org, job_id, req = stop.await_args.args
    assert org == S.org_id and str(job_id) == gate_event()["parent_job_id"]
    assert req.idempotency_key.startswith("dq-gate-") and req.drain is False
    (row,) = [r for r in await _rows(pg_db) if r.kind == "gate_event"]
    assert row.payload["fail_stop"]["status"] == "stopped" and row.status == "delivered"


async def test_gate_quarantine_never_stops_and_kill_switch_respected(pg_db):
    await qc._write_outbox(_entries(gate_event(), gate_event(action="fail_stop",
                                                             check_id="c2"), topic=GATE))
    stop = AsyncMock()
    with patch("app.services.job_service.JobService.stop", stop):
        await qc._drain_outbox(_Ucs(), _settings(ucs_quality_runs_enabled=True,
                                                 quality_gate_fail_stop_enabled=False))
    assert stop.await_count == 0
    fs = next(r for r in await _rows(pg_db) if r.payload.get("action") == "fail_stop")
    assert fs.payload["fail_stop"]["status"] == "disabled"


# ── FL-004: a short claim, then delivery with nothing held open ─────────


class _Probe(_Ucs):
    """A UCS stand-in that looks at the database from ANOTHER connection while a delivery is in flight."""

    def __init__(self, pg_db, **kw):
        super().__init__(**kw)
        self.pg_db = pg_db
        self.seen: list[dict] = []

    async def post(self, url, json=None, headers=None, timeout=None):
        async with self.pg_db() as db:
            idle_in_tx = (await db.execute(text(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                "AND pid <> pg_backend_pid() AND state LIKE 'idle in transaction%'"))).scalar()
            try:                                    # would wait for ever on a held row lock
                await db.execute(text("SET LOCAL lock_timeout = '300ms'"))
                locked = (await db.execute(text(
                    "SELECT count(*) FROM (SELECT 1 FROM quality_result_outbox FOR UPDATE) s"))).scalar()
                lock_free = locked is not None
            except Exception:  # noqa: BLE001
                lock_free = False
            await db.rollback()
        async with self.pg_db() as db:
            mine = (await db.execute(text(
                "SELECT status, lease_owner IS NOT NULL, lease_expires_at > now() FROM quality_result_outbox "
                "WHERE idempotency_key = :k"), {"k": headers["Idempotency-Key"]})).one()
        self.seen.append({"idle_in_tx": idle_in_tx, "lock_free": lock_free, "mine": tuple(mine)})
        return await super().post(url, json=json, headers=headers, timeout=timeout)


async def test_no_transaction_or_row_lock_is_held_while_delivering(pg_db):
    await qc._write_outbox(_entries(*[window_result(check_id=f"c{i}") for i in range(3)]))
    ucs = _Probe(pg_db)
    await qc._drain_outbox(ucs, _settings(ucs_quality_runs_enabled=True))
    assert len(ucs.seen) == 6
    assert all(s["idle_in_tx"] == 0 for s in ucs.seen), ucs.seen            # no pooled connection left open
    assert all(s["lock_free"] for s in ucs.seen), ucs.seen                  # no row lock held across the HTTP call
    assert all(s["mine"] == ("processing", True, True) for s in ucs.seen)   # the claim itself was already durable
    assert {r.status for r in await _rows(pg_db)} == {"delivered"}


async def test_a_slow_ucs_does_not_hold_the_database(pg_db):
    """While one worker waits on UCS, another can write results and claim different rows."""
    await qc._write_outbox(_entries(window_result()))
    slow = _Ucs(delay=0.6)
    s = _settings(ucs_quality_runs_enabled=True, quality_consumer_batch_size=1)
    first = asyncio.create_task(qc._drain_outbox(slow, s))
    await asyncio.sleep(0.2)
    await asyncio.wait_for(qc._write_outbox(_entries(window_result(check_id="c9"))), timeout=2)
    sent, _ = await qc._drain_outbox(_Ucs(), s)                             # a second worker claims another row
    assert sent == 1
    await first


async def test_a_worker_that_lost_its_claim_does_not_overwrite_the_new_owner(pg_db):
    await qc._write_outbox(_entries(window_result()))

    class _Stolen(_Ucs):
        async def post(self, url, json=None, headers=None, timeout=None):
            async with pg_db() as db:                      # the lease lapsed and another worker re-claimed the row
                await db.execute(text("SET LOCAL lock_timeout = '2s'"))
                await db.execute(update(QualityResultOutbox).values(lease_owner="someone-else"))
                await db.commit()
            return await super().post(url, json=json, headers=headers, timeout=timeout)

    await qc._drain_outbox(_Stolen(), _settings(ucs_quality_runs_enabled=True))
    assert {(r.status, r.lease_owner) for r in await _rows(pg_db)} == {("processing", "someone-else")}


async def test_a_crashed_workers_claim_is_taken_over_once_its_lease_lapses(pg_db):
    await qc._write_outbox(_entries(window_result()))
    now = datetime.now(timezone.utc)
    async with pg_db() as db:
        await db.execute(update(QualityResultOutbox).values(
            status="processing", lease_owner="dead", lease_expires_at=now + timedelta(minutes=5)))
        await db.commit()
    ucs = _Ucs()
    await qc._drain_outbox(ucs, _settings(ucs_quality_runs_enabled=True))
    assert ucs.posts == []                                                   # still held by a live-looking owner
    async with pg_db() as db:
        await db.execute(update(QualityResultOutbox).values(lease_expires_at=now - timedelta(seconds=1)))
        await db.commit()
    await qc._drain_outbox(ucs, _settings(ucs_quality_runs_enabled=True))
    assert len(ucs.posts) == 2                                               # re-delivered, same idempotency keys
    assert {r.status for r in await _rows(pg_db)} == {"delivered"}


async def test_rows_not_attempted_because_ucs_is_down_go_back_unpenalised(pg_db):
    await qc._write_outbox(_entries(*[window_result(check_id=f"c{i}") for i in range(3)]))      # 6 rows
    ucs = _Ucs(status=503)
    await qc._drain_outbox(ucs, _settings(ucs_quality_runs_enabled=True))
    assert len(ucs.posts) == 3                       # the one row's retries, then the batch stops
    rows = await _rows(pg_db)
    assert {r.status for r in rows} == {"pending"} and all(r.lease_owner is None for r in rows)
    assert sorted(r.attempts for r in rows) == [0, 0, 0, 0, 0, 1]
    assert sum(1 for r in rows if r.next_attempt_at > datetime.now(timezone.utc)) == 1


async def test_the_lease_is_extended_while_a_batch_is_delivered(pg_db):
    await qc._write_outbox(_entries(*[window_result(check_id=f"c{i}") for i in range(3)]))

    class _Watch(_Ucs):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.leases: list = []

        async def post(self, url, json=None, headers=None, timeout=None):
            async with pg_db() as db:
                self.leases.append((await db.execute(text(
                    "SELECT max(lease_expires_at) FROM quality_result_outbox WHERE status = 'processing'"))).scalar())
            return await super().post(url, json=json, headers=headers, timeout=timeout)

    ucs = _Watch(delay=0.05)
    await qc._drain_outbox(ucs, _settings(ucs_quality_runs_enabled=True))
    assert ucs.leases == sorted(ucs.leases) and ucs.leases[-1] > ucs.leases[0]


async def test_the_fail_stop_runs_with_no_transaction_open(pg_db):
    await qc._write_outbox(_entries(gate_event(action="fail_stop"), topic=GATE))
    seen = []

    async def stop(*_a, **_k):
        async with pg_db() as db:
            seen.append((await db.execute(text(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                "AND state LIKE 'idle in transaction%'"))).scalar())
            seen.append((await db.execute(text(
                "SELECT status FROM quality_result_outbox WHERE kind = 'gate_event'"))).scalar())
        return type("D", (), {"id": "dep-9"})()

    with patch("app.services.job_service.JobService.stop", stop):
        await qc._drain_outbox(_Ucs(), _settings(ucs_quality_runs_enabled=True))
    assert seen == [0, "processing"]
    (row,) = [r for r in await _rows(pg_db) if r.kind == "gate_event"]
    assert row.payload["fail_stop"]["status"] == "stopped" and row.status == "delivered"


# ── FL-005: persisted backoff, a bounded number of attempts, parking ────

def test_retry_delay_doubles_and_is_capped():
    s = _settings(quality_outbox_retry_base_seconds=30, quality_outbox_retry_max_seconds=900)
    assert [qc.retry_delay_seconds(n, s) for n in (1, 2, 3, 4, 5, 6, 7, 50)] == [30, 60, 120, 240, 480, 900, 900, 900]


async def _make_due(pg_db):
    async with pg_db() as db:
        await db.execute(update(QualityResultOutbox).where(QualityResultOutbox.status == "pending")
                         .values(next_attempt_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
        await db.commit()


async def test_a_failed_event_is_not_retried_before_its_backoff_has_passed(pg_db):
    await qc._write_outbox(_entries(window_result()))
    s = _settings(ucs_quality_runs_enabled=True, quality_outbox_retry_base_seconds=60)
    ucs = _Ucs(status=503)
    await qc._drain_outbox(ucs, s)
    first_posts = len(ucs.posts)
    rows = await _rows(pg_db)
    assert all(r.next_attempt_at > datetime.now(timezone.utc) + timedelta(seconds=40) for r in rows if r.attempts == 1)
    await qc._drain_outbox(ucs, s)                                           # the next flush: nothing is due for it
    await qc._drain_outbox(ucs, s)
    assert len(ucs.posts) > first_posts and len(ucs.posts) <= first_posts * 2   # only the OTHER (never tried) row


async def test_the_backoff_grows_with_every_failure(pg_db):
    await qc._write_outbox(_entries(window_result()))
    await _drop_started(pg_db)
    s = _settings(ucs_quality_runs_enabled=True, quality_outbox_retry_base_seconds=10,
                  quality_outbox_retry_max_seconds=100)
    delays = []
    for _ in range(5):
        await _make_due(pg_db)
        await qc._drain_outbox(_Ucs(status=503), s)
        (row,) = await _rows(pg_db)
        delays.append(round((row.next_attempt_at - datetime.now(timezone.utc)).total_seconds() / 10))
    assert delays == [1, 2, 4, 8, 10]                                        # 10, 20, 40, 80, then the 100 s cap


async def _drop_started(pg_db):
    """Keep only the completed-run row, so one event is under test."""
    async with pg_db() as db:
        await db.execute(text("DELETE FROM quality_result_outbox WHERE payload->>'status' = 'started'"))
        await db.commit()


async def test_a_poison_event_is_parked_after_the_maximum_attempts_and_never_retried(pg_db):
    await qc._write_outbox(_entries(window_result()))
    await _drop_started(pg_db)
    s = _settings(ucs_quality_runs_enabled=True, quality_outbox_max_attempts=3)
    ucs = _Ucs(status=503)
    for _ in range(3):
        await _make_due(pg_db)
        await qc._drain_outbox(ucs, s)
    (row,) = await _rows(pg_db)
    assert row.status == "parked" and row.attempts == 3
    assert "parked after 3 attempts" in row.last_error and "UCS delivery failed" in row.last_error
    posts = len(ucs.posts)
    for _ in range(3):
        await _make_due(pg_db)
        await qc._drain_outbox(ucs, s)
    assert len(ucs.posts) == posts                                           # never claimed again


async def test_a_parked_event_can_be_requeued_and_is_then_delivered(pg_db):
    await qc._write_outbox(_entries(window_result()))
    await _drop_started(pg_db)
    s = _settings(ucs_quality_runs_enabled=True, quality_outbox_max_attempts=1)
    await qc._drain_outbox(_Ucs(status=503), s)
    assert [r.status for r in await _rows(pg_db)] == ["parked"]
    async with pg_db() as db:                                                # the documented operator action
        await db.execute(text("UPDATE quality_result_outbox SET status = 'pending', attempts = 0, "
                              "next_attempt_at = now(), last_error = NULL WHERE status = 'parked'"))
        await db.commit()
    ucs = _Ucs()
    await qc._drain_outbox(ucs, s)
    assert [r.status for r in await _rows(pg_db)] == ["delivered"] and len(ucs.posts) == 1


async def test_a_poison_event_does_not_hold_up_the_events_behind_it(pg_db):
    """Head-of-line: the oldest event always fails; the rest must still flow, flush after flush."""
    await qc._write_outbox(_entries(*[window_result(check_id=f"c{i}") for i in range(4)]))
    await _drop_started(pg_db)
    poison_key = (await _rows(pg_db))[0].idempotency_key
    async with pg_db() as db:                                                # make it the oldest
        await db.execute(text("UPDATE quality_result_outbox SET created_at = now() - interval '1 day' "
                              "WHERE idempotency_key = :k"), {"k": poison_key})
        await db.commit()

    class _Poison(_Ucs):
        async def post(self, url, json=None, headers=None, timeout=None):
            self.status = 500 if headers["Idempotency-Key"] == poison_key else 201
            return await super().post(url, json=json, headers=headers, timeout=timeout)

    ucs, s = _Poison(), _settings(ucs_quality_runs_enabled=True, quality_outbox_retry_base_seconds=60)
    for _ in range(3):
        await qc._drain_outbox(ucs, s)
    by_key = {r.idempotency_key: r for r in await _rows(pg_db)}
    assert by_key[poison_key].status == "pending" and by_key[poison_key].attempts == 1
    assert sorted(r.status for k, r in by_key.items() if k != poison_key) == ["delivered"] * 3
    assert sum(1 for _, k, _ in ucs.posts if k == poison_key) == 3           # one drain's own retries, not one per flush


async def test_an_event_that_makes_delivery_raise_is_counted_and_does_not_block_the_rest(pg_db):
    await qc._write_outbox(_entries(*[window_result(check_id=f"c{i}") for i in range(2)]))
    await _drop_started(pg_db)
    async with pg_db() as db:                                      # an old, malformed row: delivery cannot build its body
        await db.execute(text("UPDATE quality_result_outbox SET payload = '{\"status\": \"completed_fail\"}'::jsonb, "
                              "created_at = now() - interval '1 day' WHERE id = (SELECT id FROM quality_result_outbox "
                              "ORDER BY created_at LIMIT 1)"))
        await db.commit()
    ucs = _Ucs()
    s = _settings()                                                # legacy mode builds the body from the payload
    sent, errors = await qc._drain_outbox(ucs, s)
    assert (sent, errors) == (1, 1) and len(ucs.posts) == 1
    bad, good = sorted(await _rows(pg_db), key=lambda r: r.status)[::-1]
    assert (bad.status, bad.attempts) == ("pending", 1) and "KeyError" in bad.last_error
    assert good.status == "delivered"


async def test_a_drain_cancelled_mid_delivery_hands_its_rows_back(pg_db):
    await qc._write_outbox(_entries(*[window_result(check_id=f"c{i}") for i in range(2)]))
    task = asyncio.create_task(qc._drain_outbox(_Ucs(delay=30), _settings(ucs_quality_runs_enabled=True)))
    await asyncio.sleep(0.4)
    assert {r.status for r in await _rows(pg_db)} == {"processing"}
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    rows = await _rows(pg_db)
    assert {r.status for r in rows} == {"pending"} and all(r.lease_owner is None and r.attempts == 0 for r in rows)
