"""FE-BUG-17: exact, bucketed failure counters — written by QualityStreamWorker
(validation) and DlqCounterWorker (ingestion), read as windowed sums."""
from collections import Counter
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services import failure_counters
from app.services.dlq_failure_counter import count_batch
from app.services.failure_counters import (
    INGESTION,
    TOTAL_KEY,
    VALIDATION,
    minute_bucket,
    prune_old_buckets,
    record_ingestion_failures,
    record_validation_failure,
    windowed_counts,
)


def test_minute_bucket_floors_to_the_minute_in_utc() -> None:
    assert minute_bucket(datetime(2026, 10, 1, 12, 34, 56, 789, tzinfo=UTC)) == datetime(2026, 10, 1, 12, 34, tzinfo=UTC)


@pytest.mark.asyncio
async def test_a_failing_record_adds_one_to_the_total_and_one_per_rule_it_broke(fake_postgres) -> None:
    await record_validation_failure(
        None, org_id="org_c1", pipeline_id="pipe-1", failed_rules=["not_null:id", "regex:email", "regex:email"],
    )

    counts = await windowed_counts("org_c1", VALIDATION, 3600)

    assert counts == {TOTAL_KEY: 1, "not_null:id": 1, "regex:email": 1}


@pytest.mark.asyncio
async def test_counts_accumulate_in_the_same_bucket_rather_than_adding_rows(fake_postgres) -> None:
    for _ in range(3):
        await record_validation_failure(None, org_id="org_c2", pipeline_id="pipe-1", failed_rules=["r1"])

    assert (await windowed_counts("org_c2", VALIDATION, 3600)) == {TOTAL_KEY: 3, "r1": 3}
    assert len(fake_postgres.failure_counts) == 2  # "*" and "r1", one bucket each


@pytest.mark.asyncio
async def test_windows_ignore_old_buckets_and_other_orgs_and_other_kinds(fake_postgres) -> None:
    old = datetime.now(UTC) - timedelta(hours=3)
    await record_validation_failure(None, org_id="org_c3", pipeline_id="p", failed_rules=["r"], at=old)
    await record_validation_failure(None, org_id="org_c3", pipeline_id="p", failed_rules=["r"])
    await record_validation_failure(None, org_id="org_other", pipeline_id="p-other", failed_rules=["r"])
    await record_ingestion_failures(
        org_id="org_c3", pipeline_id="p", counts=Counter({(minute_bucket(), "serialization"): 4}),
    )

    assert (await windowed_counts("org_c3", VALIDATION, 3600)) == {TOTAL_KEY: 1, "r": 1}
    assert (await windowed_counts("org_c3", INGESTION, 3600)) == {"serialization": 4}


@pytest.mark.asyncio
async def test_prune_drops_buckets_past_the_retention_window(fake_postgres, monkeypatch) -> None:
    monkeypatch.setattr(failure_counters.settings, "failure_count_retention_days", 7)
    ancient = datetime.now(UTC) - timedelta(days=30)
    await record_validation_failure(None, org_id="org_c4", pipeline_id="p", failed_rules=["r"], at=ancient)
    await record_validation_failure(None, org_id="org_c4", pipeline_id="p", failed_rules=["r"])

    await prune_old_buckets()

    assert all(row.bucket_start > datetime.now(UTC) - timedelta(days=1) for row in fake_postgres.failure_counts.values())


def _dlq_record(*, exception_class: str, message: str, stage: str, minutes_ago: float):
    when = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    return SimpleNamespace(
        timestamp=int(when.timestamp() * 1000),
        headers=[
            ("__connect.errors.exception.class.name", exception_class.encode()),
            ("__connect.errors.exception.message", message.encode()),
            ("__connect.errors.stage", stage.encode()),
        ],
    )


def test_count_batch_buckets_by_the_records_own_timestamp_and_cause() -> None:
    serialization = {
        "exception_class": "org.apache.kafka.connect.errors.DataException",
        "message": "Converting byte[] to Kafka Connect data failed due to serialization error",
        "stage": "VALUE_CONVERTER",
    }
    connector = {"exception_class": "java.sql.SQLException", "message": "connection reset", "stage": "TASK_PUT"}
    records = [
        _dlq_record(**serialization, minutes_ago=1),
        _dlq_record(**serialization, minutes_ago=1),
        _dlq_record(**connector, minutes_ago=1),
        _dlq_record(**connector, minutes_ago=10),
    ]

    counts = count_batch(records, oldest=datetime.now(UTC) - timedelta(days=1))

    by_category = Counter()
    for (_bucket, category), n in counts.items():
        by_category[category] += n
    assert by_category == {"serialization": 2, "connector": 2}
    assert len({bucket for bucket, _ in counts}) == 2  # the older record landed in its own, earlier minute


def test_count_batch_skips_records_older_than_the_retention_window() -> None:
    old = _dlq_record(exception_class="x", message="y", stage="TASK_PUT", minutes_ago=60 * 24 * 10)
    fresh = _dlq_record(exception_class="x", message="y", stage="TASK_PUT", minutes_ago=1)

    counts = count_batch([old, fresh], oldest=datetime.now(UTC) - timedelta(days=7))

    assert sum(counts.values()) == 1


# ── The counters' Kafka polls must never be able to starve the rest of the app's thread pool ───────────────────────

@pytest.mark.asyncio
async def test_dlq_counter_polls_run_on_their_own_pool_and_cannot_starve_the_default_one(monkeypatch) -> None:
    import asyncio
    import threading

    from app.services import dlq_failure_counter

    monkeypatch.setattr(dlq_failure_counter, "_executor", None)
    monkeypatch.setattr(dlq_failure_counter.settings, "dlq_counter_threads", 2)
    release = threading.Event()
    started = threading.Event()

    def _blocking_poll():
        started.set()
        release.wait(5)
        return threading.current_thread().name

    # More blocked polls than the counters' pool has threads — the situation of dozens of DLQ topics.
    pollers = [asyncio.create_task(dlq_failure_counter._in_dlq_pool(_blocking_poll)) for _ in range(6)]
    try:
        assert await asyncio.get_running_loop().run_in_executor(None, started.wait, 2)
        # The app's default pool (everything else's to_thread) is untouched and answers immediately.
        assert await asyncio.wait_for(asyncio.to_thread(lambda: "ok"), 1) == "ok"
    finally:
        release.set()
        names = await asyncio.gather(*pollers)
        dlq_failure_counter._executor.shutdown(wait=True)
        monkeypatch.setattr(dlq_failure_counter, "_executor", None)

    assert all(name.startswith("dlq-count") for name in names)
    assert len(set(names)) <= 2   # bounded by dlq_counter_threads, however many topics there are
