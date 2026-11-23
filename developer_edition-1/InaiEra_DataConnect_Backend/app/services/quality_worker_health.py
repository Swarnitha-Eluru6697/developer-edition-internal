"""
GAP-03: what a quality stream worker's stall looks like from the outside.

A worker never commits past a record it could not handle or dead-letter (BUG-03). If the dead-letter topic is itself down it
retries forever, so the partition stops advancing: backpressure instead of loss. That is correct, but until now it was invisible
— nothing said a partition had been stuck for an hour. This module is the visibility, and it never touches the commit decision:

  * `quality_worker_health` — a small in-process registry of every live worker's in-flight record, read at scrape time by the
    Prometheus gauges (blocked partitions, oldest uncommitted age; app/core/metrics.py).
  * `quality_stall` — a pure function over the durable columns the worker keeps on its runtime-state row. The reconciler uses it
    to report the pipeline DEGRADED once the stall passes a configured age, and to say why. It is NOT fed through Kafka Connect's
    observed state, which would make the reconciler restart a connector that is perfectly healthy.

Worker -> database: `quality_blocked_since` is stamped when the current record first fails and cleared when it commits;
`quality_blocked_heartbeat_at` is refreshed while stuck, so a worker that dies mid-stall stops counting after a few missed beats.
"""

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.config import settings
from app.core.metrics import bind_quality_gauges


@dataclass
class _Inflight:
    started: float                     # monotonic seconds when the worker took the record
    blocked_since: float | None = None  # monotonic seconds when it first failed to move past it
    partitions: int = 1                # partitions that cannot advance while it is stuck
    last_published: float = 0.0        # monotonic seconds of the last heartbeat written to the database
    published: bool = False            # whether the database currently says this worker is blocked


@dataclass
class QualityWorkerHealth:
    """Process-local and event-loop-only: every method is synchronous and cheap, and a scrape reads copies."""

    _inflight: dict[str, _Inflight] = field(default_factory=dict)

    def begin(self, worker_key: str, *, now: float | None = None) -> None:
        self._inflight[worker_key] = _Inflight(started=time.monotonic() if now is None else now)

    def mark_blocked(self, worker_key: str, *, partitions: int = 1, now: float | None = None) -> None:
        state = self._inflight.get(worker_key)
        if state is None:
            return
        if state.blocked_since is None:
            state.blocked_since = time.monotonic() if now is None else now
        state.partitions = max(1, partitions)

    def end(self, worker_key: str) -> bool:
        """The record committed (or the worker stopped). True if the database was told this worker was blocked, so the caller
        knows it must clear that."""
        state = self._inflight.pop(worker_key, None)
        return bool(state and state.published)

    def get(self, worker_key: str) -> _Inflight | None:
        return self._inflight.get(worker_key)

    def blocked_partitions(self) -> float:
        return float(sum(s.partitions for s in list(self._inflight.values()) if s.blocked_since is not None))

    def oldest_uncommitted_age(self, *, now: float | None = None) -> float:
        states = list(self._inflight.values())
        if not states:
            return 0.0
        return max(0.0, (time.monotonic() if now is None else now) - min(s.started for s in states))

    def reset(self) -> None:
        self._inflight.clear()


quality_worker_health = QualityWorkerHealth()
bind_quality_gauges(
    blocked_partitions=quality_worker_health.blocked_partitions,
    oldest_uncommitted_age=quality_worker_health.oldest_uncommitted_age,
)


@dataclass(frozen=True)
class QualityStall:
    blocked_for_seconds: float
    degraded: bool          # past settings.quality_stream_degraded_after_seconds
    message: str


def quality_stall(row: Any, *, now: datetime | None = None) -> QualityStall | None:
    """The stall this runtime-state row records, or None if there is none or its worker has stopped reporting.

    `row` needs `quality_blocked_since` and `quality_blocked_heartbeat_at` (both may be None, or absent on an older row)."""
    since = getattr(row, "quality_blocked_since", None)
    beat = getattr(row, "quality_blocked_heartbeat_at", None)
    if since is None or beat is None:
        return None
    now = now or datetime.now(UTC)
    # A worker that died mid-stall cannot clear its own flag; three missed heartbeats and it no longer counts.
    if now - beat > timedelta(seconds=settings.quality_stream_blocked_heartbeat_seconds * 3):
        return None
    blocked_for = max(0.0, (now - since).total_seconds())
    threshold = settings.quality_stream_degraded_after_seconds
    minutes = int(blocked_for // 60)
    age = f"{minutes} min" if minutes else f"{int(blocked_for)} s"
    return QualityStall(
        blocked_for_seconds=blocked_for,
        degraded=blocked_for >= threshold,
        message=(
            f"Quality checking has been stuck for {age}: a record could not be handled or written to the dead-letter topic, so the "
            "worker is holding its place instead of skipping it. No data is lost and the connector itself is running; it resumes "
            "on its own once the dead-letter topic is available again."
        ),
    )
