"""FE-BUG-17: write and read the one-minute failure-count buckets behind the
Quality screen's validation (per rule) and ingestion (per cause) breakdowns —
see models/failure_count.py for why they exist and what the keys mean.

Windows are summed over whole-minute buckets, so the edge of a window is
accurate to within one minute.
"""

from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.repositories.dataconnect import FailureCountRepository

VALIDATION = "validation"
INGESTION = "ingestion"
#: Key under kind=validation that counts failing records (not rule hits).
TOTAL_KEY = "*"


def minute_bucket(moment: datetime | None = None) -> datetime:
    moment = moment or datetime.now(UTC)
    return moment.astimezone(UTC).replace(second=0, microsecond=0)


def _rows(
    *, org_id: str, pipeline_id: str, kind: str, counts: Counter[tuple[datetime, str]],
) -> list[dict[str, Any]]:
    return [
        {"org_id": org_id, "pipeline_id": pipeline_id, "kind": kind, "key": key, "bucket_start": bucket, "count": n}
        for (bucket, key), n in counts.items()
    ]


async def record_validation_failure(
    session: Any, *, org_id: str, pipeline_id: str, failed_rules: list[str], at: datetime | None = None,
) -> None:
    """One failing record: +1 to "*" and +1 to every rule it broke. Uses the
    caller's session so it commits atomically with that caller's other writes."""
    bucket = minute_bucket(at)
    counts: Counter[tuple[datetime, str]] = Counter({(bucket, TOTAL_KEY): 1})
    for rule in set(failed_rules):
        counts[(bucket, rule)] += 1
    await FailureCountRepository(session).add_counts(
        _rows(org_id=org_id, pipeline_id=pipeline_id, kind=VALIDATION, counts=counts)
    )


async def add_ingestion_counts(
    session: Any, *, org_id: str, pipeline_id: str, counts: Counter[tuple[datetime, str]],
) -> None:
    """Pre-aggregated {(minute bucket, category): n} on the CALLER's session, so it commits atomically with the caller's other writes (the
    Kafka offsets those counts cover — see FailureCountOffset)."""
    await FailureCountRepository(session).add_counts(
        _rows(org_id=org_id, pipeline_id=pipeline_id, kind=INGESTION, counts=counts)
    )


async def record_ingestion_failures(
    *, org_id: str, pipeline_id: str, counts: Counter[tuple[datetime, str]],
) -> None:
    """Pre-aggregated {(minute bucket, category): n} from one batch of DLQ records."""
    async with AsyncSessionLocal() as session:
        await FailureCountRepository(session).add_counts(
            _rows(org_id=org_id, pipeline_id=pipeline_id, kind=INGESTION, counts=counts)
        )
        await session.commit()


async def windowed_counts(org_id: str, kind: str, window_seconds: float, pipeline_id: str | None = None) -> dict[str, int]:
    """{key: count} over the window for the whole organization, or for one pipeline when `pipeline_id` is given (always within `org_id`)."""
    since = minute_bucket(datetime.now(UTC) - timedelta(seconds=window_seconds))
    async with AsyncSessionLocal() as session:
        return await FailureCountRepository(session).sum_since(org_id, kind, since, pipeline_id)


async def windowed_counts_by_pipeline(org_id: str, kind: str, key: str, window_seconds: float) -> dict[str, int]:
    """{pipeline_id: count of `key`} over the window — e.g. how many records each pipeline failed validation on."""
    since = minute_bucket(datetime.now(UTC) - timedelta(seconds=window_seconds))
    async with AsyncSessionLocal() as session:
        return await FailureCountRepository(session).sum_since_by_pipeline(org_id, kind, key, since)


async def prune_old_buckets() -> None:
    cutoff = datetime.now(UTC) - timedelta(days=settings.failure_count_retention_days)
    async with AsyncSessionLocal() as session:
        await FailureCountRepository(session).delete_before(cutoff)
        await session.commit()
