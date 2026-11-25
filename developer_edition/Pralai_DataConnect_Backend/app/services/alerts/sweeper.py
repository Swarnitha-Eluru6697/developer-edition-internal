"""The background loop: every `alerts_sweep_interval_seconds` (60 by default) it sweeps every organization (AlertService.sweep).

Started from the app lifespan (app/main.py) next to the runtime reconciler, unless settings.alerts_sweep_enabled is off (it is in tests). Each
organization's sweep takes that organization's lease (dc_alert_state), the same one "Run rules now" takes, so any number of replicas sweep
one at a time. One organization's broken sweep never stops the others or the loop.
"""

from __future__ import annotations

import asyncio

import structlog
from sqlalchemy import select

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models import Organization
from app.services.alerts.service import AlertService

log = structlog.get_logger(__name__)


class AlertSweeper:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.ticks = 0

    async def run_once(self) -> None:
        async with AsyncSessionLocal() as session:
            org_ids = list((await session.scalars(select(Organization.org_id))).all())
        for org_id in org_ids:
            try:
                async with AsyncSessionLocal() as session:
                    summary = await AlertService(session, org_id).sweep()
                if summary.ran and (summary.raised or summary.cleared or summary.delivered or summary.detail != "ok"):
                    log.info("alerts.sweep", org_id=org_id, raised=summary.raised, cleared=summary.cleared, delivered=summary.delivered,
                             detail=summary.detail)
            except Exception:  # noqa: BLE001 - one organization's broken sweep never stops the others
                log.exception("alerts.sweep_org_failed", org_id=org_id)

    async def _loop(self) -> None:
        interval = max(10, settings.alerts_sweep_interval_seconds)
        while not self._stop.is_set():
            try:
                await self.run_once()
            except Exception:  # noqa: BLE001 - one bad sweep never ends the loop
                log.exception("alerts.sweeper_error")
            self.ticks += 1
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except TimeoutError:
                pass

    async def start(self) -> None:
        if not settings.alerts_sweep_enabled or settings.alerts_sweep_interval_seconds <= 0 or self._task is not None:
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

    def running(self) -> bool:
        return self._task is not None and not self._task.done()


alert_sweeper = AlertSweeper()
