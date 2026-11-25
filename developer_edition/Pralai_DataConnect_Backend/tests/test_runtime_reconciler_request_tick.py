"""request_tick(): a lifecycle action wakes the reconciler loop early, so "pausing"/"resuming" settles in seconds."""

import asyncio

import pytest

from app.services.runtime_reconciler import RuntimeReconciler


@pytest.mark.asyncio
async def test_request_tick_wakes_the_loop_long_before_its_interval():
    ticks: list[float] = []
    reconciler = RuntimeReconciler(client_factory=lambda: None)
    loop = asyncio.get_running_loop()

    async def fake_tick():
        ticks.append(loop.time())
        return []

    reconciler.reconcile_once = fake_tick  # type: ignore[method-assign]
    task = asyncio.create_task(reconciler.run_forever(interval_seconds=30))
    try:
        await asyncio.sleep(0.05)                      # first tick runs immediately, then the loop sleeps ~30s
        assert len(ticks) == 1
        started = loop.time()
        reconciler.request_tick(delays=(0.1,))
        await asyncio.sleep(0.5)
        assert len(ticks) == 2                          # woken by the nudge, not by the 30s interval
        assert ticks[1] - started < 1.0
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def test_request_tick_without_a_running_loop_is_a_harmless_no_op():
    RuntimeReconciler(client_factory=lambda: None).request_tick()   # the loop has never started here (tests, other process)
