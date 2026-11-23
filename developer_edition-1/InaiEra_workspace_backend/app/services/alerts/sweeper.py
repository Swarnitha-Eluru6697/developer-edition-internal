"""The background loop: a sweep every `alerts_sweep_interval_seconds` (60 by default).

Started with the API process in dev and by the worker Deployment otherwise, like the lifecycle reconciler (app.main / app.worker). It
takes the same lease as "Run rules now", so any number of replicas sweep one at a time.
"""
from __future__ import annotations

import asyncio

import structlog

from app.core.config import get_settings
from app.db.session import AsyncSessionLocal
from app.services.alerts.service import AlertService

log = structlog.get_logger()


class AlertSweeper:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.ticks = 0

    async def run_once(self) -> None:
        async with AsyncSessionLocal() as session:
            summary = await AlertService(session).sweep()
        if summary.ran and (summary.raised or summary.cleared or summary.delivered or summary.detail != "ok"):
            log.info("alerts.sweep", raised=summary.raised, cleared=summary.cleared, delivered=summary.delivered, detail=summary.detail)

    async def _loop(self) -> None:
        interval = max(10, get_settings().alerts_sweep_interval_seconds)
        while not self._stop.is_set():
            try:
                await self.run_once()
            except Exception:  # noqa: BLE001 - one bad sweep never ends the loop
                log.exception("alerts.sweeper_error")
            self.ticks += 1
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def start(self) -> None:
        s = get_settings()
        if not s.alerts_sweep_enabled or s.alerts_sweep_interval_seconds <= 0 or self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="alerts-sweeper")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        self._task = None

    def healthy(self) -> bool:
        return self._task is None or not self._task.done()


_sweeper: AlertSweeper | None = None


def get_alert_sweeper() -> AlertSweeper:
    global _sweeper
    if _sweeper is None:
        _sweeper = AlertSweeper()
    return _sweeper
